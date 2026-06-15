import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime

from dev_studio.utils.env_loader import load_env
load_env()

from flask import Flask, Response, jsonify, request, stream_with_context  # noqa: E402
from dev_studio.api import capture  # noqa: E402
from dev_studio.utils.git_utils import run_git, create_session_branch  # noqa: E402

app = Flask(__name__)
app.config["SECRET_KEY"] = "devstudio-local"


# ── LM Studio health check ────────────────────────────────────────────────────

def _lm_ping() -> tuple[bool, str | None]:
    """Makes a 1-token inference call to verify LM Studio is actually ready for use.
    Listing /v1/models is not reliable — the model can be listed but not loaded."""
    try:
        from dev_studio.crew import _BASE_URL, _API_KEY, _MODEL_ARCHITECT
        payload = json.dumps({
            "model": _MODEL_ARCHITECT,
            "messages": [{"role": "user", "content": "."}],
            "max_tokens": 1,
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            f"{_BASE_URL}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if "No models loaded" in body:
            return False, "Nenhum modelo carregado em LM Studio. Abre LM Studio > Developer e carrega o modelo."
        return False, f"LM Studio erro {e.code}: {body[:200]}"
    except urllib.error.URLError as e:
        return False, f"LM Studio inacessível: {e.reason}"
    except Exception as e:
        return False, f"Erro ao verificar LM Studio: {e}"
    return True, None

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
                defaults["running"] = False
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

# ── Agent context (automatic, server-side) ────────────────────────────────────
_last_agent_output: str = ""
_last_agent_name: str = ""


# ── SSE ───────────────────────────────────────────────────────────────────────

@app.route("/stream")
def stream():
    def generate():
        yield f"data: {json.dumps({'type': 'session_state', 'session': _safe_session()})}\n\n"
        while True:
            try:
                msg = capture.sse_queue.get(timeout=20)
                yield f"data: {json.dumps(msg)}\n\n"
            except Exception:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _safe_session():
    return {k: v for k, v in session.items() if k != "running"}


# ── CORS (para dev Angular em porta diferente) ────────────────────────────────

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.route("/api/<path:p>", methods=["OPTIONS"])
@app.route("/stream", methods=["OPTIONS"])
def options_handler(p=""):
    return "", 204


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/status")
def status():
    project_path = session.get("project_path", "")
    has_changes = False
    if project_path and os.path.exists(project_path):
        diff = run_git(["diff", "--stat"], project_path)
        untracked = run_git(["ls-files", "--others", "--exclude-standard"], project_path)
        has_changes = bool(diff.stdout.strip() or untracked.stdout.strip())
    return jsonify({"session": _safe_session(), "running": session["running"], "has_changes": has_changes})


@app.route("/api/session/start", methods=["POST"])
def session_start():
    data = request.json or {}
    project_path = data.get("project_path", "").strip() or session["project_path"]
    if not project_path or not os.path.exists(project_path):
        return jsonify({"error": f"Caminho não encontrado: {project_path}"}), 400

    session["project_path"] = project_path
    session["started_at"] = datetime.now().isoformat()
    session["requests"] = []

    branch, _ = create_session_branch(project_path)
    session["branch"] = branch
    _save_session()

    capture.sse_queue.put({"type": "session_start", "project": project_path, "branch": branch, "started_at": session["started_at"]})
    return jsonify({"ok": True, "branch": branch})


@app.route("/api/session/clear", methods=["POST"])
def session_clear():
    session.update({"project_path": "", "branch": None, "requests": [], "started_at": None})
    _save_session()
    return jsonify({"ok": True})


@app.route("/api/request/run-agent", methods=["POST"])
def run_agent_endpoint():
    global _last_agent_output, _last_agent_name
    if session["running"]:
        return jsonify({"error": "Agente já está a correr. Aguarda."}), 409
    data = request.json or {}
    agent = data.get("agent", "architect").strip()
    user_request = data.get("request", "").strip()
    if not user_request:
        return jsonify({"error": "Pedido vazio"}), 400
    if not session["project_path"]:
        return jsonify({"error": "Sessão não iniciada"}), 400
    if agent not in ("architect", "developer", "reviewer", "dev+review"):
        return jsonify({"error": f"Agente desconhecido: {agent}"}), 400

    req_num = len(session["requests"]) + 1
    entry = {"num": req_num, "request": user_request, "agent": agent, "status": "running", "elapsed": None}
    session["requests"].append(entry)
    session["running"] = True

    _AGENT_LABELS = {
        "architect": "Arquitecto", "developer": "Developer",
        "reviewer": "Reviewer", "dev+review": "Developer + Reviewer",
    }
    label = _AGENT_LABELS.get(agent, agent)
    capture.sse_queue.put({"type": "request_start", "num": req_num, "request": user_request, "agent": agent, "agent_label": label})

    context_snapshot = _last_agent_output
    context_from_snapshot = _last_agent_name

    def _run():
        global _last_agent_output, _last_agent_name
        from dev_studio.crew import DevStudioCrew  # noqa: E402
        project_path = session["project_path"]
        inputs = {"request": user_request, "project_path": project_path, "context": context_snapshot}
        start = time.time()
        try:
            ok, err = _lm_ping()
            if not ok:
                capture.sse_queue.put({"type": "lm_error", "text": err})
                entry["status"] = "error"
                return

            crew = DevStudioCrew(project_path=project_path)
            if agent == "architect":
                result = crew.crew().kickoff(inputs=inputs)
            elif agent == "developer":
                result = crew.implement_crew().kickoff(inputs=inputs)
            elif agent == "reviewer":
                result = crew.review_crew().kickoff(inputs=inputs)
            elif agent == "dev+review":
                result = crew.dev_review_crew().kickoff(inputs=inputs)

            raw = str(result.raw) if hasattr(result, "raw") else str(result)
            _last_agent_output = raw
            _last_agent_name = agent
            capture.sse_queue.put({"type": "context_updated", "from_agent": agent, "agent_label": label})
            entry["status"] = "done"
        except KeyboardInterrupt:
            capture.sse_queue.put({"type": "output", "text": "\nPedido cancelado pelo utilizador.\n"})
            entry["status"] = "cancelled"
        except Exception as e:
            import traceback
            if "No models loaded" in str(e):
                capture.sse_queue.put({"type": "lm_error", "text": "LM Studio: modelo descarregado. Carrega o modelo e tenta novamente."})
            else:
                capture.sse_queue.put({"type": "output", "text": f"\nErro: {e}\n{traceback.format_exc()}\n"})
            entry["status"] = "error"
        finally:
            entry["elapsed"] = round(time.time() - start)
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


@app.route("/api/request/cancel", methods=["POST"])
def cancel_request():
    if not session["running"]:
        return jsonify({"error": "Nenhum pedido em execução"}), 400
    capture._cancel_requested.set()
    return jsonify({"ok": True})


@app.route("/api/context/clear", methods=["POST"])
def context_clear():
    global _last_agent_output, _last_agent_name
    _last_agent_output = ""
    _last_agent_name = ""
    capture.sse_queue.put({"type": "context_updated", "from_agent": None, "agent_label": None})
    return jsonify({"ok": True})


@app.route("/api/context/status", methods=["GET"])
def context_status():
    return jsonify({
        "has_context": bool(_last_agent_output),
        "from_agent": _last_agent_name or None,
        "length": len(_last_agent_output),
    })


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
    run_git(["add", "-A"], project_path)
    result = run_git(["commit", "-m", full_msg], project_path)
    if result.returncode == 0:
        return jsonify({"ok": True, "branch": branch})
    return jsonify({"error": result.stderr.strip()}), 500


@app.route("/api/branches")
def list_branches():
    project_path = session.get("project_path", "")
    if not project_path or not os.path.exists(project_path):
        return jsonify({"branches": [], "current": None})
    result = run_git(["branch"], project_path)
    if result.returncode != 0:
        return jsonify({"branches": [], "current": None})
    branches, current = [], None
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
    result = run_git(["branch", "-D" if force else "-d", name], project_path)
    if result.returncode == 0:
        was_session = name == session.get("branch")
        if was_session:
            session["branch"] = None
            session["requests"] = []
            _save_session()
        return jsonify({"ok": True, "was_session_branch": was_session})
    stderr = result.stderr.strip()
    if "not fully merged" in stderr:
        return jsonify({"error": "unmerged", "detail": stderr}), 409
    return jsonify({"error": stderr}), 500


def _lm_quick_check() -> tuple[bool, str | None, str | None]:
    """Fast check via /v1/models — returns (ok, error, model_name)."""
    try:
        from dev_studio.crew import _BASE_URL, _API_KEY
        req = urllib.request.Request(
            f"{_BASE_URL}/models",
            headers={"Authorization": f"Bearer {_API_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            models = data.get("data", [])
            if not models:
                return False, "Nenhum modelo carregado.", None
            model_name = models[0].get("id", "desconhecido")
            return True, None, model_name
    except urllib.error.URLError as e:
        return False, f"LM Studio inacessível: {e.reason}", None
    except Exception as e:
        return False, f"Erro: {e}", None


@app.route("/api/lm-status", methods=["GET"])
def lm_status():
    ok, err, model = _lm_quick_check()
    return jsonify({"ok": ok, "error": err, "model": model})


# ── Entry point ───────────────────────────────────────────────────────────────

def run_server(host: str = "127.0.0.1", port: int = 7777, open_browser: bool = False):
    capture.enable()
    print(f"\n🚀  Dev Studio API → http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
