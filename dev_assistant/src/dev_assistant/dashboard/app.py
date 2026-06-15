import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime

# Load .env before any crewai import
_env_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from dev_assistant.dashboard import capture

app = Flask(__name__)
app.config["SECRET_KEY"] = "devassistant-local"

_SESSION_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".crew_session.json")

# ── Session state ─────────────────────────────────────────────────────────────

def _load_session() -> dict:
    defaults = {
        "project_path": os.environ.get("TARGET_PROJECT", ""),
        "branch": None,
        "requests": [],
        "running": False,
        "started_at": None,
    }
    try:
        if os.path.exists(_SESSION_FILE):
            with open(_SESSION_FILE, encoding="utf-8") as f:
                saved = json.load(f)
                defaults.update(saved)
                defaults["running"] = False  # never persist running=True
    except Exception:
        pass
    return defaults

def _save_session():
    try:
        data = {k: v for k, v in session.items() if k != "running"}
        with open(_SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

session = _load_session()


# ── Git helpers ───────────────────────────────────────────────────────────────

def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def _create_branch(project_path: str) -> str | None:
    if _git(["status"], project_path).returncode != 0:
        return None
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    branch = f"crew/session-{stamp}"
    result = _git(["checkout", "-b", branch], project_path)
    return branch if result.returncode == 0 else None


# ── SSE endpoint ──────────────────────────────────────────────────────────────

@app.route("/stream")
def stream():
    def generate():
        # Send current session state immediately on connect
        yield f"data: {json.dumps({'type': 'session_state', 'session': _safe_session()})}\n\n"
        while True:
            try:
                msg = capture.sse_queue.get(timeout=20)
                yield f"data: {json.dumps(msg)}\n\n"
            except Exception:
                # Keepalive ping
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _safe_session():
    return {k: v for k, v in session.items() if k != "running"}


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/session/start", methods=["POST"])
def session_start():
    data = request.json or {}
    project_path = data.get("project_path", "").strip() or session["project_path"]
    if not project_path or not os.path.exists(project_path):
        return jsonify({"error": f"Caminho não encontrado: {project_path}"}), 400

    session["project_path"] = project_path
    session["started_at"] = datetime.now().isoformat()
    session["requests"] = []

    branch = _create_branch(project_path)
    session["branch"] = branch
    _save_session()

    capture.sse_queue.put({
        "type": "session_start",
        "project": project_path,
        "branch": branch,
        "started_at": session["started_at"],
    })
    return jsonify({"ok": True, "branch": branch})


@app.route("/api/request/run", methods=["POST"])
def run_request():
    if session["running"]:
        return jsonify({"error": "Crew já está a correr. Aguarda."}), 409
    data = request.json or {}
    feature_request = data.get("request", "").strip()
    if not feature_request:
        return jsonify({"error": "Pedido vazio"}), 400
    if not session["project_path"]:
        return jsonify({"error": "Sessão não iniciada"}), 400

    req_num = len(session["requests"]) + 1
    entry = {"num": req_num, "request": feature_request, "status": "🔄", "elapsed": None}
    session["requests"].append(entry)
    session["running"] = True

    capture.sse_queue.put({"type": "request_start", "num": req_num, "request": feature_request})

    def _run():
        from dev_assistant.crew import DevAssistantCrew
        project_path = session["project_path"]
        base_inputs = {
            "feature_request": feature_request,
            "project_path": project_path,
            "architect_plan": "",
            "review_feedback": "",
        }
        start = time.time()
        try:
            crew = DevAssistantCrew(project_path=project_path)

            # ── Phase 1: Design ──
            capture.sse_queue.put({"type": "output", "text": "\n🏗️  FASE 1/3 — Architect\n"})
            design_result = crew.crew().kickoff(inputs=base_inputs)
            architect_plan = str(design_result.raw) if hasattr(design_result, "raw") else str(design_result)

            # Pause for user approval — intercepted by dashboard modal
            approval = input("\n✅  Plano acima. Aprovas a implementação? (s/n): ").strip().lower()
            if approval != "s":
                capture.sse_queue.put({"type": "output", "text": "⛔  Implementação cancelada pelo utilizador."})
                entry["status"] = "⛔"
                return

            # ── Phase 2: Implement ──
            capture.sse_queue.put({"type": "output", "text": "\n💻  FASE 2/3 — Developer\n"})
            impl_inputs = {**base_inputs, "architect_plan": architect_plan}
            crew2 = DevAssistantCrew(project_path=project_path)
            crew2.implement_crew().kickoff(inputs=impl_inputs)

            # ── Phase 3: Review ──
            capture.sse_queue.put({"type": "output", "text": "\n🔍  FASE 3/3 — Reviewer\n"})
            crew3 = DevAssistantCrew(project_path=project_path)
            review_result = crew3.review_crew().kickoff(inputs=impl_inputs)
            review_raw = str(review_result.raw) if hasattr(review_result, "raw") else str(review_result)

            if "Requer correções" in review_raw or "❌" in review_raw:
                capture.sse_queue.put({"type": "fix_available", "feedback": review_raw})

            entry["status"] = "✅"
        except Exception as e:
            import traceback
            capture.sse_queue.put({"type": "output", "text": f"\n❌ Erro: {e}\n{traceback.format_exc()}\n"})
            entry["status"] = "❌"
        finally:
            elapsed = time.time() - start
            entry["elapsed"] = round(elapsed)
            session["running"] = False
            _save_session()
            capture.sse_queue.put({
                "type": "request_done",
                "num": req_num,
                "status": entry["status"],
                "elapsed": entry["elapsed"],
                "requests": session["requests"],
            })

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "num": req_num})


@app.route("/api/request/fix", methods=["POST"])
def run_fix():
    if session["running"]:
        return jsonify({"error": "Crew já está a correr"}), 409
    data = request.json or {}
    feedback = data.get("feedback", "")
    feature_request = session["requests"][-1]["request"] if session["requests"] else ""
    session["running"] = True

    def _fix():
        from dev_assistant.crew import DevAssistantCrew
        inputs = {
            "feature_request": feature_request,
            "project_path": session["project_path"],
            "architect_plan": "",
            "review_feedback": feedback,
        }
        try:
            DevAssistantCrew(project_path=session["project_path"]).fix_crew().kickoff(inputs=inputs)
        except Exception as e:
            capture.sse_queue.put({"type": "output", "text": f"\n❌ Erro: {e}\n"})
        finally:
            session["running"] = False
            capture.sse_queue.put({"type": "fix_done"})

    threading.Thread(target=_fix, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/input/respond", methods=["POST"])
def input_respond():
    data = request.json or {}
    response = data.get("response", "")
    try:
        capture.input_response_queue.put_nowait(response)
        capture.sse_queue.put({"type": "input_done"})
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"error": "Nenhum input pendente"}), 400


@app.route("/api/session/commit", methods=["POST"])
def session_commit():
    data = request.json or {}
    project_path = session["project_path"]
    branch = session["branch"]
    custom_msg = data.get("message", "").strip()

    requests_done = [r for r in session["requests"] if r["status"] == "✅"]
    summary = "\n".join(f"- {r['request'][:80]}" for r in requests_done)
    default_msg = f"crew session: {len(requests_done)} pedido(s) completado(s)"
    msg = custom_msg or default_msg
    full_msg = f"{msg}\n\n{summary}" if summary else msg

    _git(["add", "-A"], project_path)
    result = _git(["commit", "-m", full_msg], project_path)
    if result.returncode == 0:
        capture.sse_queue.put({"type": "output", "text": f"✅ Commit feito na branch {branch}"})
        return jsonify({"ok": True, "branch": branch})
    else:
        return jsonify({"error": result.stderr.strip()}), 500


@app.route("/api/status")
def status():
    project_path = session.get("project_path", "")
    has_changes = False
    if project_path and os.path.exists(project_path):
        diff = _git(["diff", "--stat"], project_path)
        untracked = _git(["ls-files", "--others", "--exclude-standard"], project_path)
        has_changes = bool(diff.stdout.strip() or untracked.stdout.strip())
    return jsonify({
        "session": _safe_session(),
        "running": session["running"],
        "has_changes": has_changes,
    })


@app.route("/api/branches")
def list_branches():
    project_path = session.get("project_path", "")
    if not project_path or not os.path.exists(project_path):
        return jsonify({"branches": [], "current": None})
    result = _git(["branch"], project_path)
    if result.returncode != 0:
        return jsonify({"branches": [], "current": None})
    branches = []
    current = None
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        is_current = line.startswith("*")
        name = line.lstrip("* ").strip()
        if is_current:
            current = name
        branches.append({"name": name, "current": is_current})
    return jsonify({"branches": branches, "current": current})


@app.route("/api/branches/delete", methods=["POST"])
def delete_branch():
    data = request.json or {}
    name = data.get("name", "").strip()
    force = data.get("force", False)
    project_path = session.get("project_path", "")
    if not name or not project_path:
        return jsonify({"error": "Parâmetros inválidos"}), 400
    flag = "-D" if force else "-d"
    result = _git(["branch", flag, name], project_path)
    if result.returncode == 0:
        was_session_branch = (name == session.get("branch"))
        if was_session_branch:
            session["branch"] = None
            session["requests"] = []
            _save_session()
        return jsonify({"ok": True, "was_session_branch": was_session_branch})
    # If safe-delete fails due to unmerged, return specific error
    stderr = result.stderr.strip()
    if "not fully merged" in stderr:
        return jsonify({"error": "unmerged", "detail": stderr}), 409
    return jsonify({"error": stderr}), 500


@app.route("/api/session/clear", methods=["POST"])
def session_clear():
    session.update({"project_path": "", "branch": None, "requests": [], "started_at": None})
    _save_session()
    return jsonify({"ok": True})


@app.route("/")
def index():
    return render_template("index.html",
                           default_project=os.environ.get("TARGET_PROJECT", ""))


# ── Entry point ───────────────────────────────────────────────────────────────

def run_server(host: str = "127.0.0.1", port: int = 7777, open_browser: bool = True):
    capture.enable()
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    print(f"\n🌐  DevAssistant Dashboard → http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
