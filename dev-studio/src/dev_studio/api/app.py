import ctypes
import json
import logging
import os

# Load .env before anything else (no-op if file doesn't exist)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env"))
except ImportError:
    pass
import queue
import re
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

# ── Structured logging ────────────────────────────────────────────────────────
_LOG_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "dev_studio.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("dev_studio.api")


# ── LM Studio health check ────────────────────────────────────────────────────

def _lm_ping() -> tuple[bool, str | None]:
    """Makes a 1-token inference call to verify LM Studio is actually ready for use.
    Listing /v1/models is not reliable — the model can be listed but not loaded."""
    try:
        from dev_studio.utils.settings import load_settings
        s = load_settings()
        payload = json.dumps({
            "model": s["model_name"] or "default",
            "messages": [{"role": "user", "content": "."}],
            "max_tokens": 1,
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            f"{s['lm_base_url']}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {s['lm_api_key']}",
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


def _lm_keepalive() -> None:
    """Fire-and-forget 1-token inference to prevent LM Studio from auto-unloading the model.
    Called in a background thread during plan approval wait so the idle timer never fires."""
    try:
        from dev_studio.utils.settings import load_settings
        s = load_settings()
        payload = json.dumps({
            "model": s["model_name"] or "default",
            "messages": [{"role": "user", "content": "."}],
            "max_tokens": 1,
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            f"{s['lm_base_url']}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {s['lm_api_key']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=20)
    except Exception:
        pass  # non-fatal — next tick will retry


_SESSION_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".crew_session.json")
_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".crew_history.json")
_session_lock = threading.Lock()
_lang_cache: dict = {"path": "", "mtime": 0.0, "langs": []}


def _git_index_mtime(project_path: str) -> float:
    """Return mtime of .git/index, or 0.0 if not a git repo or on error."""
    try:
        git_index = os.path.join(project_path, ".git", "index")
        return os.path.getmtime(git_index) if os.path.exists(git_index) else 0.0
    except OSError:
        return 0.0


def _detect_languages(project_path: str) -> list[str]:
    """Detect source languages in project by scanning file extensions.

    Result is cached and invalidated when .git/index changes (i.e. any
    git add / commit / checkout that adds or removes tracked files).
    """
    global _lang_cache
    if not project_path or not os.path.exists(project_path):
        return []
    mtime = _git_index_mtime(project_path)
    if _lang_cache["path"] == project_path and _lang_cache["mtime"] == mtime:
        return _lang_cache["langs"]
    _SKIP = {'node_modules', '.git', '__pycache__', '.venv', 'venv', 'dist', 'build',
             '.idea', '.vs', 'bin', 'obj', '.next', '.nuxt', 'coverage'}
    _EXT: dict[str, str] = {
        '.py': 'Python',
        '.ts': 'TypeScript', '.tsx': 'TypeScript',
        '.js': 'JavaScript', '.jsx': 'JavaScript', '.mjs': 'JavaScript',
        '.ps1': 'PowerShell', '.psm1': 'PowerShell', '.psd1': 'PowerShell',
        '.cs': 'C#',
        '.java': 'Java', '.kt': 'Kotlin',
        '.go': 'Go', '.rs': 'Rust',
        '.rb': 'Ruby', '.php': 'PHP',
        '.swift': 'Swift', '.dart': 'Dart',
        '.cpp': 'C++', '.cc': 'C++',
        '.c': 'C',
    }
    _ORDER = ['Python', 'Angular', 'TypeScript', 'JavaScript', 'PowerShell',
              'C#', 'Java', 'Kotlin', 'Go', 'Rust', 'Ruby', 'PHP', 'Swift', 'Dart', 'C++', 'C']
    counts: dict[str, int] = {}
    has_angular = False
    try:
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in _SKIP]
            for f in files:
                if f == 'angular.json':
                    has_angular = True
                _, ext = os.path.splitext(f.lower())
                lang = _EXT.get(ext)
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1
    except Exception:
        pass
    if has_angular and 'TypeScript' in counts:
        counts['Angular'] = counts.pop('TypeScript')
    top = {lang for lang, _ in sorted(counts.items(), key=lambda x: -x[1])[:4]}
    result = [l for l in _ORDER if l in top]
    _lang_cache = {"path": project_path, "mtime": mtime, "langs": result}
    return result


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
                for req in defaults.get("requests", []):
                    if req.get("status") == "running":
                        req["status"] = "error"
                    if req.get("elapsed") is None:
                        req["elapsed"] = 0
    except Exception:
        pass
    return defaults


def _load_history(project_path: str) -> list:
    """Return saved requests for a specific project from the history file."""
    try:
        if os.path.exists(_HISTORY_FILE):
            with open(_HISTORY_FILE, encoding="utf-8") as f:
                all_histories: dict = json.load(f)
            entries = all_histories.get(project_path, [])
            for req in entries:
                if req.get("status") == "running":
                    req["status"] = "error"
                if req.get("elapsed") is None:
                    req["elapsed"] = 0
            return entries
    except Exception:
        pass
    return []


def _save_history():
    """Persist current session's requests into the per-project history file."""
    project_path = session.get("project_path", "")
    if not project_path:
        return
    try:
        all_histories: dict = {}
        if os.path.exists(_HISTORY_FILE):
            with open(_HISTORY_FILE, encoding="utf-8") as f:
                all_histories = json.load(f)
        all_histories[project_path] = session["requests"]
        with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(all_histories, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _save_session():
    try:
        data = {k: v for k, v in session.items() if k != "running"}
        with open(_SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    _save_history()


session = _load_session()

# ── Agent context (automatic, server-side) ────────────────────────────────────
_last_agent_output: str = ""
_last_agent_name: str = ""

# ── Active crew thread (for hard cancel) ──────────────────────────────────────
_active_crew_thread: threading.Thread | None = None


def _interrupt_crew_thread() -> bool:
    """Inject KeyboardInterrupt into the active crew thread via ctypes.
    Works between Python bytecodes, including between LLM streaming chunks.
    Returns True if a thread was targeted."""
    t = _active_crew_thread
    if t is None or not t.is_alive():
        return False
    tid = t.ident
    if tid is None:
        return False
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(tid),
        ctypes.py_object(KeyboardInterrupt),
    )
    if res > 1:
        # More than one thread affected — undo immediately
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)
        return False
    return True

# ── Project map cache ─────────────────────────────────────────────────────────
# Both caches are keyed on (project_path, git_index_mtime) so they invalidate
# automatically after any git add / commit / checkout that touches tracked files.
_scan_cache:  dict = {"path": "", "mtime": 0.0, "map": ""}
_state_cache: dict = {"path": "", "mtime": 0.0, "state": None, "summary": ""}


def _get_project_map(project_path: str) -> str:
    from dev_studio.tools.project_scanner_tool import scan_project_sync  # lazy import
    global _scan_cache
    mtime = _git_index_mtime(project_path)
    if (_scan_cache["path"] == project_path
            and _scan_cache["mtime"] == mtime
            and _scan_cache["map"]):
        return _scan_cache["map"]
    project_map = scan_project_sync(project_path)
    _scan_cache = {"path": project_path, "mtime": mtime, "map": project_map}
    return project_map


def _get_project_state(project_path: str) -> tuple[object, str]:
    """Return (state, summary_text), cached by git-index mtime + dirty flag.

    Invalidates when .git/index changes (staged files) OR when the working
    tree has unstaged modifications, so the state stays accurate after any
    tool writes a file even before the user runs git add.
    Returns (None, "") on error so callers can gracefully degrade.
    """
    from dev_studio.tools.project_state_tool import write_project_state, summarize_state
    global _state_cache
    mtime = _git_index_mtime(project_path)
    try:
        dirty_flag = bool(run_git(["status", "--porcelain"], project_path).stdout.strip())
    except Exception:
        dirty_flag = False
    cache_key = (project_path, mtime, dirty_flag)
    if (_state_cache.get("key") == cache_key
            and _state_cache["state"] is not None):
        return _state_cache["state"], _state_cache["summary"]
    state   = write_project_state(project_path)
    summary = summarize_state(state)
    _state_cache = {"key": cache_key, "path": project_path, "mtime": mtime,
                    "state": state, "summary": summary}
    return state, summary


# ── SSE ───────────────────────────────────────────────────────────────────────

@app.route("/stream")
def stream():
    def generate():
        try:
            yield f"data: {json.dumps({'type': 'session_state', 'session': _safe_session()})}\n\n"
            while True:
                try:
                    msg = capture.sse_queue.get(timeout=20)
                    yield f"data: {json.dumps(msg)}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        except GeneratorExit:
            pass  # client disconnected — exit cleanly without error logging

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
    return jsonify({"session": _safe_session(), "running": session["running"], "has_changes": has_changes,
                    "languages": _detect_languages(project_path)})


def _validate_and_clean(result: object, raw: str, agent_name: str) -> str:
    """Strip issues lacking evidence snippets; re-serialize to JSON if changed.
    Returns the (potentially cleaned) raw string passed to the next agent."""
    try:
        from dev_studio.models import ArchitecturePlan, ReviewResult
        pydantic_out = getattr(result, "pydantic", None)
        if pydantic_out is None:
            capture.sse_queue.put({"type": "output", "text":
                "\n⚠️  AVISO: output não estruturado (pydantic ausente).\n"})
            return raw

        if isinstance(pydantic_out, ArchitecturePlan):
            if pydantic_out.changes and not pydantic_out.files_read:
                # Only flag if the architect proposed changes without reading files
                capture.sse_queue.put({"type": "output", "text":
                    "\n⛔ VALIDAÇÃO: Arquitecto propôs alterações sem ler nenhum ficheiro (files_read vazio)."
                    " O plano pode ser baseado em suposições.\n"})
            no_proof = [i for i in pydantic_out.issues if not i.snippet]
            if no_proof:
                pydantic_out.issues = [i for i in pydantic_out.issues if i.snippet]
                capture.sse_queue.put({"type": "output", "text":
                    f"\n⛔ VALIDAÇÃO: {len(no_proof)} issue(s) REMOVIDOS (sem snippet de prova)."
                    f" Restam {len(pydantic_out.issues)} issue(s) válidos para o Developer.\n"})
                return pydantic_out.model_dump_json()

        elif isinstance(pydantic_out, ReviewResult):
            no_proof = [i for i in pydantic_out.issues if not i.snippet]
            changed = False
            if no_proof:
                pydantic_out.issues = [i for i in pydantic_out.issues if i.snippet]
                capture.sse_queue.put({"type": "output", "text":
                    f"\n⛔ VALIDAÇÃO: {len(no_proof)} issue(s) REMOVIDOS (sem snippet de prova)."
                    f" Restam {len(pydantic_out.issues)} issue(s) válidos.\n"})
                changed = True
            if not pydantic_out.approved and not pydantic_out.issues:
                pydantic_out.approved = True
                pydantic_out.verdict = "Aprovado (sem evidências concretas de falha)"
                capture.sse_queue.put({"type": "output", "text":
                    "\n⛔ VALIDAÇÃO: Reviewer rejeitou sem evidências válidas"
                    " — forçado Aprovado.\n"})
                changed = True
            if changed:
                return pydantic_out.model_dump_json()

    except Exception:
        pass
    return raw


def _validate_snippets_exist(arch_raw: str, project_path: str) -> tuple[str, list[str]]:
    """Validate that snippets from architect's issues actually exist in the files.

    Returns (possibly cleaned JSON, list of warning messages).
    Strips issues whose snippets don't match the actual file content.
    Also validates line numbers are within file bounds.
    """
    warnings = []
    try:
        import json
        d = json.loads(arch_raw)
        issues = d.get("issues", [])
        valid_issues = []

        for issue in issues:
            file_path = issue.get("file", "")
            line_str = issue.get("line", "")
            snippet = issue.get("snippet", "")

            if not file_path or not snippet:
                valid_issues.append(issue)
                continue

            # Resolve file path
            resolved = file_path
            if not os.path.isabs(resolved):
                resolved = os.path.join(project_path, resolved)
            resolved = os.path.normpath(resolved)

            if not os.path.exists(resolved):
                warnings.append(f"Issue ignorado: ficheiro não existe: {file_path}")
                continue

            # Read the file and check if snippet exists
            try:
                with open(resolved, encoding="utf-8", errors="replace") as f:
                    content = f.read()
                total_lines = len(content.splitlines())

                # Validate line numbers are within bounds
                if line_str:
                    # Parse line numbers (e.g., "42" or "42-55")
                    try:
                        if "-" in str(line_str):
                            start_line, end_line = map(int, str(line_str).split("-"))
                        else:
                            start_line = end_line = int(line_str)

                        if start_line > total_lines:
                            warnings.append(
                                f"Issue ignorado: linha {start_line} excede tamanho do ficheiro "
                                f"{os.path.basename(file_path)} ({total_lines} linhas)"
                            )
                            continue
                    except (ValueError, TypeError):
                        pass  # Line parsing failed, continue with snippet check

                # Check if snippet exists in file (exact or normalized)
                if snippet in content:
                    valid_issues.append(issue)
                else:
                    # Try normalized comparison
                    norm_content = "\n".join(line.rstrip() for line in content.splitlines())
                    norm_snippet = "\n".join(line.rstrip() for line in snippet.splitlines())
                    if norm_snippet in norm_content:
                        valid_issues.append(issue)
                    else:
                        warnings.append(
                            f"Issue ignorado: snippet não encontrado em {os.path.basename(file_path)} "
                            f"(linha {line_str}). Snippet: {snippet[:50]}..."
                        )
            except Exception as e:
                warnings.append(f"Erro ao ler {file_path}: {e}")
                valid_issues.append(issue)  # Keep on error

        if len(valid_issues) < len(issues):
            d["issues"] = valid_issues
            return json.dumps(d, ensure_ascii=False), warnings

    except Exception:
        pass
    return arch_raw, warnings


def _fix_json_escapes(s: str) -> str:
    """Fix invalid JSON backslash escapes produced by LLMs writing Windows paths.

    Uses a state machine instead of a regex so it doesn't misfire on valid
    escape sequences that coincidentally appear in Windows paths (e.g. \\t in
    C:\\tests\\ would be touched by a naive negative-lookahead regex).

    Valid JSON escapes after \\: " \\ / b f n r t u
    Everything else gets doubled: \\X → \\\\X
    """
    result: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '\\' and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in '"\\/ bfnrtu':
                result.append(ch)   # keep valid escape as-is
            else:
                result.append('\\\\')  # double the backslash
            i += 1
        else:
            result.append(ch)
        i += 1
    return ''.join(result)


def _recover_arch_json(exc: Exception) -> str | None:
    """Walk the exception chain from a failed kickoff and recover the raw JSON.
    Pydantic v2 ValidationError stores the input_value on json_invalid errors —
    we extract it, fix escape sequences, and return the corrected JSON string."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if hasattr(cur, "errors"):
            try:
                for err in cur.errors():  # type: ignore[union-attr]
                    if err.get("type") == "json_invalid":
                        raw = err.get("input", "")
                        if isinstance(raw, str) and "{" in raw:
                            start = raw.find("{")
                            return _fix_json_escapes(raw[start:])
            except Exception:
                pass
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    return None


def _norm_patch(text: str) -> str:
    """Normalise for matching: strip trailing whitespace + CRLF→LF."""
    return "\n".join(l.rstrip() for l in text.splitlines())


def _stripped_find(content: str, snippet: str) -> str | None:
    """Find snippet by stripped-line comparison (ignores leading/trailing whitespace).
    Returns the actual file text or None if not found/ambiguous."""
    c_lines = content.splitlines(keepends=True)
    s_lines = snippet.splitlines()
    n = len(s_lines)
    if n == 0:
        return None
    c_sigs = [l.strip() for l in c_lines]
    s_sigs = [l.strip() for l in s_lines]
    matches: list[tuple[int, int]] = []
    for i in range(len(c_lines) - n + 1):
        if c_sigs[i:i + n] == s_sigs:
            matches.append((i, i + n))
    if len(matches) == 1:
        return "".join(c_lines[matches[0][0]:matches[0][1]])
    return None


def _apply_single_patch(
    content: str, snippet: str, patch_after: str, line_hint: str
) -> tuple[bool, str]:
    """Apply one snippet→patch_after replacement using 4 strategies in order.

    Returns (True, new_content) on success or (False, error_message) on failure.

    Strategy 1 — exact match
    Strategy 2 — normalised (trailing ws stripped, CRLF→LF)
    Strategy 3 — line-anchored (uses issue.line ± 5 lines, normalised comparison)
    Strategy 4 — stripped-line (ignores all leading/trailing whitespace per line)
    """
    # 1 — exact
    if snippet in content:
        if content.count(snippet) > 1:
            return False, f"Snippet ambíguo (aparece {content.count(snippet)}× no ficheiro)"
        return True, content.replace(snippet, patch_after, 1)

    # 2 — normalised (handles CRLF and trailing spaces)
    nc = _norm_patch(content)
    ns = _norm_patch(snippet)
    if ns and ns in nc:
        if nc.count(ns) > 1:
            return False, f"Snippet ambíguo (normalizado, {nc.count(ns)}×)"
        start = nc.index(ns)
        end   = start + len(ns)
        return True, nc[:start] + _norm_patch(patch_after) + nc[end:]

    # 3 — line-anchored: use issue.line to find exact location (± 5 lines tolerance)
    if line_hint:
        try:
            start_line = int(line_hint.split("-")[0]) - 1  # 0-indexed
            c_lines    = content.splitlines(keepends=True)
            s_lines    = snippet.splitlines()
            n          = len(s_lines)
            for offset in range(-5, 6):
                pos = start_line + offset
                if pos < 0 or pos + n > len(c_lines):
                    continue
                block = "".join(c_lines[pos:pos + n])
                if _norm_patch(block) == _norm_patch(snippet):
                    new_content = "".join(c_lines[:pos]) + patch_after + "".join(c_lines[pos + n:])
                    return True, new_content
        except (ValueError, IndexError):
            pass

    # 4 — stripped-line: tolerates indentation differences from model-generated snippets
    actual_old = _stripped_find(content, snippet)
    if actual_old is not None:
        if content.count(actual_old) > 1:
            return False, f"Correspondência aproximada ambígua ({content.count(actual_old)}×)"
        return True, content.replace(actual_old, patch_after, 1)

    # All strategies failed
    first   = snippet.strip().splitlines()[0][:50] if snippet.strip() else ""
    similar = [l.rstrip() for l in content.splitlines() if first[:20] in l][:3]
    hint    = ("\n  Linhas semelhantes: " + " | ".join(similar)) if similar else ""
    return False, f"Snippet não encontrado (4 estratégias tentadas).{hint}"


def _apply_patches_from_plan(issues: list[dict], project_path: str) -> tuple[list[str], list[str]]:
    """Apply patch_after directly from Architect issues (VIA DIRECTA — sem Developer LLM).

    Uses 4-strategy matching per patch: exact → normalised → line-anchored → stripped-line.
    Groups patches by file and writes each file exactly once.
    Returns (files_modified, errors).
    """
    from collections import defaultdict

    by_file: dict[str, list[dict]] = defaultdict(list)
    for issue in issues:
        if issue.get("snippet", "").strip() and issue.get("patch_after", "").strip():
            by_file[issue["file"]].append(issue)

    files_modified: list[str] = []
    errors: list[str] = []

    for filepath, file_issues in by_file.items():
        try:
            with open(filepath, encoding="utf-8") as f:
                content = f.read()
        except Exception as exc:
            msg = f"Não foi possível ler '{os.path.basename(filepath)}': {exc}"
            errors.append(msg)
            capture.sse_queue.put({"type": "output", "text": f"  ❌ {msg}\n"})
            continue

        modified    = content
        file_errors: list[str] = []

        for issue in file_issues:
            ok, result = _apply_single_patch(
                modified,
                issue["snippet"],
                issue["patch_after"],
                issue.get("line", ""),
            )
            if ok:
                modified = result
            else:
                file_errors.append(f"  linha {issue.get('line', '?')}: {result}")

        if file_errors:
            err_msg = (
                f"{os.path.basename(filepath)} — {len(file_errors)} patch(es) falharam:\n"
                + "\n".join(file_errors)
            )
            errors.append(err_msg)
            capture.sse_queue.put({"type": "output", "text": f"  ❌ {err_msg}\n"})
            continue

        if modified == content:
            msg = f"{os.path.basename(filepath)}: nenhuma alteração detectada"
            errors.append(msg)
            capture.sse_queue.put({"type": "output", "text": f"  ⚠️ {msg}\n"})
            continue

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(modified)
            n = len(file_issues)
            files_modified.append(filepath)
            capture.sse_queue.put({"type": "output", "text":
                f"  ✅ {os.path.basename(filepath)} — {n} patch(es) aplicado(s)\n"})
        except Exception as exc:
            msg = f"Falha ao escrever '{os.path.basename(filepath)}': {exc}"
            errors.append(msg)
            capture.sse_queue.put({"type": "output", "text": f"  ❌ {msg}\n"})

    return files_modified, errors


def _pre_flight_warnings(project_path: str) -> list[str]:
    """Return non-blocking warnings before starting a full flow run."""
    warns: list[str] = []
    # Warn about very large projects (slow scanning / context overflow risk)
    pm = _scan_cache.get("map", "")
    if pm:
        file_count = pm.count("\n")
        if file_count > 400:
            warns.append(f"Projeto tem ~{file_count} ficheiros — pode ser lento e exceder o contexto do modelo.")
    # Warn about untracked/modified git files that could conflict with patches
    try:
        dirty = run_git(["diff", "--name-only", "--diff-filter=M"], project_path).stdout.strip()
        if dirty:
            first_five = ", ".join(dirty.splitlines()[:5])
            extra = f" +{len(dirty.splitlines()) - 5} mais" if len(dirty.splitlines()) > 5 else ""
            warns.append(f"Ficheiros modificados não commitados: {first_five}{extra}. Patches podem conflituar.")
    except Exception:
        pass
    return warns


def _trim_plan_for_developer(
    arch_raw: str,
    approved_indices: list[int] | None = None,
    approved_issue_indices: list[int] | None = None,
) -> str:
    """Build the minimal context for the Developer agent.

    Keeps only what the Developer actually needs:
    - issues (with snippets — the Developer uses these for fast patch_file)
    - changes (file paths and actions)
    - plan (ANTES/DEPOIS markdown — allows patch_file without read_file first)

    Drops files_read and endpoints_verified — the Developer never uses them and
    they inflate the context window significantly.

    approved_indices: filter changes to only the approved subset (None = all)
    approved_issue_indices: filter issues to only the selected subset (None = all)
    Falls back to the original string if arch_raw is not valid JSON.
    """
    try:
        d = json.loads(arch_raw)
        all_changes = d.get("changes", [])
        all_issues  = d.get("issues", [])

        changes = ([all_changes[i] for i in approved_indices if i < len(all_changes)]
                   if approved_indices is not None else all_changes)

        issues  = ([all_issues[i]  for i in approved_issue_indices if i < len(all_issues)]
                   if approved_issue_indices is not None else all_issues)

        # Strip snippet + patch_after from issues — both are already in plan ANTES/DEPOIS.
        # Keeps file/line/description/severity so Developer knows where to look,
        # but avoids duplicating code blocks already in the plan field.
        issues_compact = [
            {k: v for k, v in issue.items() if k not in ("snippet", "patch_after")}
            for issue in issues
        ]

        # Cap plan field to prevent Developer context overflow.
        # For comparison tasks, the architect writes the full diff in plan (incl.
        # device-specific differences that are NOT real issues). Capping prevents
        # the first Developer LLM call from exceeding the model's context window.
        # Issues list (file/line/description) is always preserved — Developer can
        # use VIA LENTA (read_file → patch_file) for any section not in plan.
        _PLAN_CAP = 5000
        plan_text = d.get("plan", "")
        if len(plan_text) > _PLAN_CAP:
            plan_text = (
                plan_text[:_PLAN_CAP]
                + f"\n\n⚠️ [PLANO TRUNCADO — original: {len(d.get('plan', ''))} chars, "
                f"máx: {_PLAN_CAP}. "
                "Para issues sem ANTES/DEPOIS acima: usa VIA LENTA "
                "(read_file na linha indicada → patch_file / patch_file_multi).]\n"
            )

        return json.dumps({
            "issues":  issues_compact,
            "changes": changes,
            "plan":    plan_text,
        }, ensure_ascii=False, indent=2)
    except Exception:
        return arch_raw


def _emit_result_card(result: object, agent: str) -> None:
    """Emit a formatted result summary to the SSE stream after any agent completes."""
    try:
        from dev_studio.models import ArchitecturePlan, ImplementationResult, ReviewResult
        pydantic_out = getattr(result, "pydantic", None)
        if pydantic_out is None:
            return
        sep = "─" * 52
        lines: list[str] = [f"\n{sep}"]

        if isinstance(pydantic_out, ImplementationResult):
            total = len(pydantic_out.files_modified) + len(pydantic_out.files_created)
            lines.append(f"## Developer — {total} ficheiro(s) alterado(s)")
            if pydantic_out.files_modified:
                lines.append(f"### Modificados ({len(pydantic_out.files_modified)})")
                lines.extend(f"- {f}" for f in pydantic_out.files_modified)
            if pydantic_out.files_created:
                lines.append(f"### Criados ({len(pydantic_out.files_created)})")
                lines.extend(f"- {f}" for f in pydantic_out.files_created)
            if pydantic_out.syntax_errors:
                lines.append(f"### Erros de Sintaxe ({len(pydantic_out.syntax_errors)})")
                lines.extend(
                    f"- {e.file}:{e.line} — {e.description}"
                    for e in pydantic_out.syntax_errors
                )
            if pydantic_out.summary:
                lines.append("### Resumo")
                lines.append(pydantic_out.summary)

        elif isinstance(pydantic_out, ReviewResult):
            status = "APROVADO" if pydantic_out.approved else "REQUER CORREÇÕES"
            lines.append(f"## Reviewer — {status}")
            lines.append(pydantic_out.verdict)
            if pydantic_out.issues:
                lines.append(f"### Problemas ({len(pydantic_out.issues)})")
                lines.extend(
                    f"- [{i.severity.upper()}] {i.file}:{i.line} — {i.description}"
                    for i in pydantic_out.issues
                )

        elif isinstance(pydantic_out, ArchitecturePlan):
            lines.append("## Arquitecto — Análise")
            if pydantic_out.issues:
                lines.append(f"### Problemas encontrados ({len(pydantic_out.issues)})")
                lines.extend(
                    f"- [{i.severity.upper()}] {i.file}:{i.line} — {i.description}"
                    for i in pydantic_out.issues
                )
            if pydantic_out.changes:
                lines.append(f"### Alterações planeadas ({len(pydantic_out.changes)})")
                lines.extend(f"- {c.action.upper()} {c.path}" for c in pydantic_out.changes)
        else:
            return

        capture.sse_queue.put({"type": "output", "text": "\n".join(lines) + "\n"})
    except Exception:
        pass


def _emit_full_flow_summary(
    arch_result: object,
    dev_result:  object,
    rev_result:  object | None = None,
    fix_result:  object | None = None,
    rev2_result: object | None = None,
    backend_files: list[str] | None = None,
) -> None:
    """Emit the combined Resumo Final after a Fluxo Completo run."""
    try:
        from dev_studio.models import ArchitecturePlan, ImplementationResult, ReviewResult
        sep = "═" * 52
        lines: list[str] = [f"\n{sep}", "## Fluxo Completo — Resumo Final", sep]

        # ── Planeado (Arquitecto) ──────────────────────────────────────────────
        arch_plan = getattr(arch_result, "pydantic", None)
        if isinstance(arch_plan, ArchitecturePlan) and arch_plan.changes:
            lines.append(f"### Planeado ({len(arch_plan.changes)} alteração/alterações)")
            lines.extend(f"- {c.action.upper()} {c.path}" for c in arch_plan.changes)

        # ── Implementado (Backend VIA DIRECTA ou Developer LLM) ───────────────
        dev_impl = getattr(dev_result, "pydantic", None)
        if backend_files is not None:
            lines.append(f"### Patchado pelo Backend ({len(backend_files)} ficheiro(s))")
            lines.extend(f"- PATCHADO {f}" for f in backend_files)
        elif isinstance(dev_impl, ImplementationResult):
            total = len(dev_impl.files_modified) + len(dev_impl.files_created)
            lines.append(f"### Implementado ({total} ficheiro(s))")
            lines.extend(f"- MODIFICADO {f}" for f in dev_impl.files_modified)
            lines.extend(f"- CRIADO {f}" for f in dev_impl.files_created)
            if dev_impl.syntax_errors:
                lines.append(f"### Erros de Sintaxe ({len(dev_impl.syntax_errors)})")
                lines.extend(
                    f"- {e.file}:{e.line} — {e.description}"
                    for e in dev_impl.syntax_errors
                )

        # ── Revisão inicial ────────────────────────────────────────────────────
        rev_pd = getattr(rev_result, "pydantic", None) if rev_result else None
        if isinstance(rev_pd, ReviewResult):
            status = "✅ APROVADO" if rev_pd.approved else f"❌ {len(rev_pd.issues)} problema(s)"
            lines.append(f"### Revisão: {status}")

        # ── Ciclo de correcção (se houve) ──────────────────────────────────────
        fix_impl = getattr(fix_result, "pydantic", None) if fix_result else None
        if isinstance(fix_impl, ImplementationResult):
            total_fix = len(fix_impl.files_modified) + len(fix_impl.files_created)
            lines.append(f"### Correcção ({total_fix} ficheiro(s) após revisão)")
            lines.extend(f"- MODIFICADO {f}" for f in fix_impl.files_modified)
            lines.extend(f"- CRIADO {f}" for f in fix_impl.files_created)

        rev2_pd = getattr(rev2_result, "pydantic", None) if rev2_result else None
        if isinstance(rev2_pd, ReviewResult):
            final = "✅ APROVADO" if rev2_pd.approved else f"⚠️ {len(rev2_pd.issues)} problema(s) por resolver"
            lines.append(f"### Revisão Final: {final}")
            if not rev2_pd.approved and rev2_pd.issues:
                for i in rev2_pd.issues[:3]:
                    lines.append(f"  - {i.description}")
                if len(rev2_pd.issues) > 3:
                    lines.append(f"  ... e mais {len(rev2_pd.issues) - 3} problema(s)")

        # ── Resumo do Developer (mais recente) ─────────────────────────────────
        last_impl = fix_impl if isinstance(fix_impl, ImplementationResult) else dev_impl
        if isinstance(last_impl, ImplementationResult) and last_impl.summary:
            lines.append("### Resumo do Developer")
            lines.append(last_impl.summary)

        capture.sse_queue.put({"type": "output", "text": "\n".join(lines) + "\n"})
    except Exception:
        pass


@app.route("/api/session/start", methods=["POST"])
def session_start():
    data = request.json or {}
    project_path = data.get("project_path", "").strip() or session["project_path"]
    if not project_path or not os.path.exists(project_path):
        return jsonify({"error": f"Caminho não encontrado: {project_path}"}), 400

    # Persist current project's history before switching to the new one.
    _save_history()

    with _session_lock:
        session["project_path"] = project_path
        session["started_at"] = datetime.now().isoformat()
        session["requests"] = _load_history(project_path)

    branch, _ = create_session_branch(project_path)
    initial_commit = run_git(["rev-parse", "HEAD"], project_path).stdout.strip() or ""
    with _session_lock:
        session["branch"] = branch
        session["initial_commit"] = initial_commit
    _save_session()

    capture.sse_queue.put({"type": "session_start", "project": project_path, "branch": branch,
                           "started_at": session["started_at"], "languages": _detect_languages(project_path),
                           "requests": session["requests"]})
    return jsonify({"ok": True, "branch": branch})


@app.route("/api/session/clear", methods=["POST"])
def session_clear():
    project_path = session.get("project_path", "")
    if project_path:
        try:
            if os.path.exists(_HISTORY_FILE):
                with open(_HISTORY_FILE, encoding="utf-8") as f:
                    all_histories: dict = json.load(f)
                if project_path in all_histories:
                    del all_histories[project_path]
                    with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
                        json.dump(all_histories, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    with _session_lock:
        session.update({"project_path": "", "branch": None, "requests": [], "started_at": None,
                        "initial_commit": ""})
    _save_session()
    return jsonify({"ok": True})


@app.route("/api/session/rollback", methods=["POST"])
def session_rollback():
    """git reset --hard to the commit that existed when this session was started."""
    with _session_lock:
        initial_commit = session.get("initial_commit", "")
        project_path   = session.get("project_path",   "")
        running        = session.get("running",         False)
    if running:
        return jsonify({"error": "Agente a correr — aguarda antes de fazer rollback"}), 409
    if not initial_commit:
        return jsonify({"error": "Sem commit inicial guardado para esta sessão"}), 400
    if not project_path:
        return jsonify({"error": "Sessão não iniciada"}), 400
    try:
        out = run_git(["reset", "--hard", initial_commit], project_path)
        logger.info("Session rollback to %s: %s", initial_commit, out.strip())
        capture.sse_queue.put({"type": "output", "text":
            f"✅ Rollback para commit {initial_commit[:8]}.\n{out}\n"})
        return jsonify({"ok": True, "commit": initial_commit, "output": out})
    except Exception as e:
        logger.exception("Rollback failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/session/set-context", methods=["POST"])
def session_set_context():
    """Restore _last_agent_output from a previous request's stored output."""
    global _last_agent_output, _last_agent_name
    data = request.json or {}
    num  = data.get("num")
    with _session_lock:
        entry = next((r for r in session["requests"] if r.get("num") == num), None)
    if not entry or not entry.get("output"):
        return jsonify({"error": "Pedido não encontrado ou sem output armazenado"}), 404
    _last_agent_output = entry["output"]
    _last_agent_name   = entry.get("agent", "")
    label = f"Pedido #{num}"
    capture.sse_queue.put({"type": "context_updated", "from_agent": _last_agent_name,
                           "agent_label": label})
    return jsonify({"ok": True})


# ── Settings (LM Studio URL / key / model) ───────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def get_settings():
    from dev_studio.utils.settings import load_settings
    return jsonify(load_settings())


@app.route("/api/settings", methods=["POST"])
def post_settings():
    from dev_studio.utils.settings import save_settings
    data = request.json or {}
    try:
        save_settings(data)
        logger.info("Settings updated: %s", {k: v for k, v in data.items() if k != "lm_api_key"})
        return jsonify({"ok": True})
    except Exception as e:
        logger.exception("Failed to save settings")
        return jsonify({"error": str(e)}), 500


@app.route("/api/request/run-agent", methods=["POST"])
def run_agent_endpoint():
    global _last_agent_output, _last_agent_name
    data = request.json or {}
    agent = data.get("agent", "architect").strip()
    user_request = data.get("request", "").strip()
    if not user_request:
        return jsonify({"error": "Pedido vazio"}), 400
    if not session["project_path"]:
        return jsonify({"error": "Sessão não iniciada"}), 400
    if agent not in ("architect", "developer", "reviewer", "dev+review", "arch+review"):
        return jsonify({"error": f"Agente desconhecido: {agent}"}), 400

    with _session_lock:
        if session["running"]:
            return jsonify({"error": "Agente já está a correr. Aguarda."}), 409
        req_num = len(session["requests"]) + 1
        entry = {"num": req_num, "request": user_request, "agent": agent, "status": "running", "elapsed": None}
        session["requests"].append(entry)
        session["running"] = True

    _AGENT_LABELS = {
        "architect":   "Arquitecto",
        "developer":   "Developer",
        "reviewer":    "Reviewer",
        "dev+review":  "Developer + Reviewer",
        "arch+review": "Arquitecto + Reviewer",
    }
    label = _AGENT_LABELS.get(agent, agent)
    capture.sse_queue.put({"type": "request_start", "num": req_num, "request": user_request, "agent": agent, "agent_label": label})

    context_snapshot = _last_agent_output
    context_from_snapshot = _last_agent_name

    def _run():
        global _last_agent_output, _last_agent_name, _active_crew_thread
        _active_crew_thread = threading.current_thread()
        from dev_studio.crew import DevStudioCrew  # noqa: E402
        project_path = session["project_path"]

        # Build the project map (cached by git-index mtime — fast on repeat requests).
        capture.sse_queue.put({"type": "scanning", "text": "A gerar PROJECT MAP..."})
        project_map = _get_project_map(project_path)
        capture.sse_queue.put({"type": "scanning_done", "text": "PROJECT MAP gerado"})
        capture.sse_queue.put({"type": "output", "text": project_map + "\n"})

        # Build structured project state index (deterministic, no LLM).
        # Writes .crew_project_state.json to project root for agent tool use.
        try:
            capture.sse_queue.put({"type": "scanning", "text": "A analisar estrutura do projeto..."})
            _, state_summary = _get_project_state(project_path)
            capture.sse_queue.put({"type": "scanning_done", "text": "Estrutura analisada"})
            capture.sse_queue.put({"type": "output", "text": state_summary + "\n"})
        except Exception as _e:
            capture.sse_queue.put({"type": "scanning_done", "text": f"⚠️ Análise falhou: {_e}"})
            capture.sse_queue.put({"type": "output", "text": f"⚠️ Project state scanner falhou: {_e}\n"})

        context_with_map = (
            f"{project_map}\n\n{context_snapshot}"
            if context_snapshot
            else project_map
        )
        inputs = {
            "request": user_request,
            "project_path": project_path,
            "context": context_with_map,
        }
        start = time.time()
        try:
            ok, err = _lm_ping()
            if not ok:
                capture.sse_queue.put({"type": "lm_error", "text": err})
                entry["status"] = "error"
                return

            crew = DevStudioCrew(project_path=project_path)
            _run_inputs = inputs
            if agent == "arch+review":
                _run_inputs = {"request": user_request, "project_path": project_path, "context": project_map}

            _crew_fn = {
                "architect":   crew.crew,
                "developer":   crew.implement_crew,
                "reviewer":    crew.review_crew,
                "dev+review":  crew.dev_review_crew,
                "arch+review": crew.arch_review_crew,
            }.get(agent)
            if _crew_fn is None:
                entry["status"] = "error"
                return

            try:
                result = _crew_fn().kickoff(inputs=_run_inputs)
                raw = str(result.raw) if hasattr(result, "raw") else str(result)
            except Exception as _ke:
                recovered = _recover_arch_json(_ke)
                if recovered is None:
                    raise
                capture.sse_queue.put({"type": "output", "text":
                    "⚠️ JSON inválido (paths Windows) — corrigido automaticamente.\n"})
                result = None
                raw = recovered
            raw = _validate_and_clean(result, raw, agent)
            _emit_result_card(result, agent)
            _last_agent_output = raw
            _last_agent_name = agent
            entry["output"] = raw
            capture.sse_queue.put({"type": "context_updated", "from_agent": agent, "agent_label": label})
            entry["status"] = "done"
        except KeyboardInterrupt:
            capture.sse_queue.put({"type": "output", "text": "\nPedido cancelado pelo utilizador.\n"})
            entry["status"] = "cancelled"
        except Exception as e:
            import traceback
            logger.exception("run_agent error (agent=%s req=%s)", agent, req_num)
            if "No models loaded" in str(e):
                capture.sse_queue.put({"type": "lm_error", "text":
                    "Nenhum modelo carregado em LM Studio.\n"
                    "Solução: Abre LM Studio → Developer → carrega o modelo e tenta novamente."})
            else:
                capture.sse_queue.put({"type": "output", "text": f"\nErro: {e}\n{traceback.format_exc()}\n"})
            entry["status"] = "error"
        finally:
            capture._cancel_requested.clear()
            _active_crew_thread = None
            entry["elapsed"] = round(time.time() - start)
            with _session_lock:
                session["running"] = False
            _save_session()
            stats = dict(capture._request_tool_stats)
            if stats:
                capture.sse_queue.put({"type": "session_stats", "stats": stats})
            capture._request_tool_stats.clear()
            capture.sse_queue.put({
                "type": "request_done",
                "num": req_num,
                "status": entry["status"],
                "elapsed": entry["elapsed"],
                "requests": session["requests"],
            })

    capture._request_tool_stats.clear()
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "num": req_num})


@app.route("/api/request/cancel", methods=["POST"])
def cancel_request():
    if not session["running"]:
        return jsonify({"error": "Nenhum pedido em execução"}), 400
    capture._cancel_requested.set()
    injected = _interrupt_crew_thread()
    logger.info("Cancel requested — thread interrupt: %s", injected)
    return jsonify({"ok": True, "injected": injected})


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
    with _session_lock:
        project_path = session.get("project_path", "")
        branch       = session.get("branch", "")
    data = request.json or {}
    custom_msg = data.get("message", "").strip()
    # Sanitize: strip characters that break git commit -m on command line.
    # Null bytes crash git; bare carriage returns corrupt the message.
    # Limit the subject line to 72 chars to follow git conventions.
    custom_msg = custom_msg.replace('\x00', '').replace('\r', '')
    if '\n' in custom_msg:
        # Keep multi-line messages but strip leading blank lines
        custom_msg = custom_msg.lstrip('\n')
    requests_done = [r for r in session["requests"] if r["status"] == "✅"]
    summary = "\n".join(f"- {r['request'][:80]}" for r in requests_done)
    default_msg = f"crew session: {len(requests_done)} pedido(s) completado(s)"
    msg = custom_msg or default_msg
    full_msg = f"{msg}\n\n{summary}" if summary else msg
    # Smart git add: stage only the files the developer agent reported touching.
    # Falls back to git add -A if the last output can't be parsed or has no paths.
    files_to_add: list[str] = []
    parse_err: str | None = None
    try:
        parsed = json.loads(_last_agent_output)
        files_to_add = (parsed.get("files_modified", []) or []) + (parsed.get("files_created", []) or [])
    except Exception as _pe:
        parse_err = str(_pe)
    if files_to_add:
        run_git(["add", "--"] + files_to_add, project_path)
    else:
        if parse_err:
            capture.sse_queue.put({"type": "output", "text":
                f"⚠️ Smart git add falhou (output não é JSON válido) — a usar git add -A\n"})
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
    """Fast reachability check via /v1/models. Any 2xx = ok (model validation
    happens inside _lm_ping when the request actually runs)."""
    try:
        from dev_studio.utils.settings import load_settings
        s = load_settings()
        req = urllib.request.Request(
            f"{s['lm_base_url']}/models",
            headers={"Authorization": f"Bearer {s['lm_api_key']}"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read()
            try:
                data   = json.loads(body)
                models = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                model_name = models[0].get("id", "desconhecido") if models else None
            except Exception:
                model_name = None
            return True, None, model_name
    except urllib.error.HTTPError as e:
        return False, f"LM Studio HTTP {e.code}", None
    except urllib.error.URLError as e:
        return False, f"LM Studio inacessível: {e.reason}", None
    except Exception as e:
        return False, f"Erro: {e}", None


@app.route("/api/lm-status", methods=["GET"])
def lm_status():
    ok, err, model = _lm_quick_check()
    return jsonify({"ok": ok, "error": err, "model": model})


# ── Full-flow plan approval state ─────────────────────────────────────────────
# Using an Event + plain variable instead of a Queue eliminates the race where
# a stale API call (e.g. double-click) could inject a second decision between
# the queue.empty() drain loop and the blocking get(), silently overriding the
# user's real choice.

_plan_approval_event    = threading.Event()
_plan_approval_decision: dict | None = None   # set atomically by approve/reject endpoints
_plan_approval_lock     = threading.Lock()    # guards _plan_approval_decision writes
_pending_plan_data: dict | None = None


_CONN_MARKERS      = ("Connection error", "ConnectError", "10051", "10061")
_CRASH_MARKERS     = ("model has crashed", "The model has crashed", "18446744072635812000", "No models loaded")
_CONVERTER_MARKERS = ("ConverterError", "convert text into a Pydantic model")

_PARTIAL_DEV_JSON = (
    '{"files_modified":[],"files_created":[],"syntax_errors":[],'
    '"summary":"⚠️ Developer interrompido por limite de iterações (max_iter) — '
    'algumas alterações podem estar incompletas."}'
)


def _wait_for_model_reload(sse_put, timeout: int = 600, poll: int = 30) -> None:
    """Emit crash notification and poll LM Studio until model is back online.

    Raises RuntimeError on timeout so the caller can propagate the failure.
    """
    sse_put({"type": "lm_error", "text":
        "\n💥 O modelo rebentou (GPU OOM ou erro interno do LM Studio).\n"
        "➡️  Abre o LM Studio → Developer e recarrega o modelo.\n"
        f"⏳ A aguardar que o modelo volte online (máx. {timeout // 60} min)...\n"})
    waited = 0
    while waited < timeout:
        time.sleep(poll)
        waited += poll
        ok, _ = _lm_ping()
        if ok:
            sse_put({"type": "output", "text":
                f"✅ Modelo disponível novamente após {waited}s. A retomar o agente...\n"})
            return
        sse_put({"type": "output", "text":
            f"   ⏳ Modelo ainda offline... ({waited}s / {timeout}s)\n"})
    sse_put({"type": "output", "text":
        f"⛔ Tempo esgotado ({timeout}s). Cancela e recarrega o modelo manualmente.\n"})
    raise RuntimeError("Timeout aguardando modelo após crash")


def _kickoff_developer(crew_fn, inputs: dict):
    """Kickoff developer/fix crew; recover gracefully from ConverterError (max_iter hit)."""
    try:
        result = _kickoff_with_retry(
            lambda: crew_fn().kickoff(inputs=inputs),
            capture.sse_queue.put,
        )
        raw = str(result.raw) if hasattr(result, "raw") else str(result)
        return result, raw
    except Exception as e:
        if any(m in str(e) or m in type(e).__name__ for m in _CONVERTER_MARKERS):
            capture.sse_queue.put({"type": "output", "text":
                "\n⚠️ Developer atingiu o limite de iterações — algumas alterações podem "
                "estar incompletas. A continuar para o Reviewer...\n"})
            return None, _PARTIAL_DEV_JSON
        raise


def _kickoff_with_retry(kickoff_fn, sse_put, max_attempts: int = 6, delay: int = 10):
    """Retry on connection errors (up to max_attempts); wait for model reload on crash (once)."""
    crash_retried = False
    attempt = 0
    while True:
        attempt += 1
        try:
            return kickoff_fn()
        except Exception as e:
            err_str = str(e)

            # Model crash (GPU OOM / internal error): wait for reload, retry once
            if any(m in err_str for m in _CRASH_MARKERS) and not crash_retried:
                crash_retried = True
                _wait_for_model_reload(sse_put)  # raises RuntimeError on timeout
                continue  # retry without counting as a connection-error attempt

            if any(m in err_str for m in _CONN_MARKERS) and attempt < max_attempts:
                sse_put({"type": "output", "text":
                    f"⚠️ LM Studio inacessível (tentativa {attempt}/{max_attempts}), "
                    f"a aguardar {delay}s...\n"})
                time.sleep(delay)
            else:
                raise


@app.route("/api/request/full-flow", methods=["POST"])
def full_flow_endpoint():
    global _last_agent_output, _last_agent_name, _pending_plan_data
    data = request.json or {}
    user_request = data.get("request", "").strip()
    if not user_request:
        return jsonify({"error": "Pedido vazio"}), 400
    if not session["project_path"]:
        return jsonify({"error": "Sessão não iniciada"}), 400

    with _session_lock:
        if session["running"]:
            return jsonify({"error": "Agente já está a correr. Aguarda."}), 409
        req_num = len(session["requests"]) + 1
        entry = {"num": req_num, "request": user_request, "agent": "full-flow", "status": "running", "elapsed": None}
        session["requests"].append(entry)
        session["running"] = True
    capture.sse_queue.put({
        "type": "request_start", "num": req_num,
        "request": user_request, "agent": "full-flow",
        "agent_label": "Fluxo Completo",
    })
    context_snapshot = _last_agent_output

    def _run():
        global _last_agent_output, _last_agent_name, _pending_plan_data, _active_crew_thread, _plan_approval_decision
        _active_crew_thread = threading.current_thread()
        from dev_studio.crew import DevStudioCrew
        project_path = session["project_path"]
        start = time.time()
        capture._request_tool_stats.clear()
        try:
            ok, err = _lm_ping()
            if not ok:
                capture.sse_queue.put({"type": "lm_error", "text": err})
                entry["status"] = "error"
                return

            # ── Pre-flight warnings (non-blocking) ────────────────────────────
            for warn in _pre_flight_warnings(project_path):
                capture.sse_queue.put({"type": "output", "text": f"⚠️ {warn}\n"})

            # ── Project scan (cached by git-index mtime) ───────────────────────
            capture.sse_queue.put({"type": "scanning", "text": "A gerar PROJECT MAP..."})
            project_map = _get_project_map(project_path)
            capture.sse_queue.put({"type": "scanning_done", "text": "PROJECT MAP gerado"})
            capture.sse_queue.put({"type": "output", "text": project_map + "\n"})
            try:
                capture.sse_queue.put({"type": "scanning", "text": "A analisar estrutura do projeto..."})
                _, state_summary = _get_project_state(project_path)
                capture.sse_queue.put({"type": "scanning_done", "text": "Estrutura analisada"})
                capture.sse_queue.put({"type": "output", "text": state_summary + "\n"})
            except Exception as _e:
                capture.sse_queue.put({"type": "scanning_done", "text": f"⚠️ Análise falhou: {_e}"})
                capture.sse_queue.put({"type": "output", "text": f"⚠️ Project state scanner falhou: {_e}\n"})

            # Arquitecto recebe APENAS o project_map — não o context_snapshot do agente anterior.
            # O Full Flow é sempre uma análise nova: o Arquitecto lê os ficheiros por si mesmo.
            # Incluir o output anterior inflaria o contexto e podia causar crash de GPU (OOM).
            inputs = {"request": user_request, "project_path": project_path, "context": project_map}

            # ── Fase 1/3: Arquitecto ───────────────────────────────────────────
            capture.sse_queue.put({"type": "output", "text": "\nFase 1/3 — Arquitecto a analisar...\n"})
            crew = DevStudioCrew(project_path=project_path)
            try:
                arch_result = _kickoff_with_retry(
                    lambda: crew.crew().kickoff(inputs=inputs),
                    capture.sse_queue.put,
                )
                arch_raw = str(arch_result.raw) if hasattr(arch_result, "raw") else str(arch_result)
            except Exception as _kickoff_err:
                # LLMs sometimes write Windows paths like C:\_PROJECTS\ inside JSON
                # strings without escaping the backslash — fix and recover.
                recovered = _recover_arch_json(_kickoff_err)
                if recovered is None:
                    raise
                capture.sse_queue.put({"type": "output", "text":
                    "⚠️ Output do Arquitecto continha JSON inválido (paths Windows) — corrigido automaticamente.\n"})
                arch_result = None
                arch_raw = recovered
            arch_raw = _validate_and_clean(arch_result, arch_raw, "architect")
            _emit_result_card(arch_result, "architect")

            # ── Validate snippets exist in actual files ────────────────────────
            arch_raw, snippet_warnings = _validate_snippets_exist(arch_raw, project_path)
            for warn in snippet_warnings:
                capture.sse_queue.put({"type": "output", "text": f"⚠️ {warn}\n"})
            if snippet_warnings:
                capture.sse_queue.put({"type": "output", "text":
                    f"\n🔧 {len(snippet_warnings)} issue(s) removidos (snippets não encontrados no ficheiro actual).\n"})

            # ── Validate changes file paths exist ──────────────────────────────
            try:
                d = json.loads(arch_raw)
                changes = d.get("changes", [])
                valid_changes = []
                for change in changes:
                    path = change.get("path", "")
                    if not path:
                        valid_changes.append(change)
                        continue
                    resolved = path
                    if not os.path.isabs(resolved):
                        resolved = os.path.join(project_path, resolved)
                    resolved = os.path.normpath(resolved)
                    if os.path.exists(resolved):
                        valid_changes.append(change)
                    else:
                        capture.sse_queue.put({"type": "output", "text":
                            f"⚠️ Change ignorado: ficheiro não existe: {path}\n"})
                if len(valid_changes) < len(changes):
                    d["changes"] = valid_changes
                    arch_raw = json.dumps(d, ensure_ascii=False)
            except Exception:
                pass

            # Extract structured plan dict for the approval modal
            plan_pydantic = getattr(arch_result, "pydantic", None)
            if plan_pydantic is not None:
                plan_dict = plan_pydantic.model_dump()
            else:
                try:
                    plan_dict = json.loads(arch_raw)
                except Exception:
                    plan_dict = {
                        "files_read": [], "issues": [], "changes": [],
                        "endpoints_verified": [], "plan": arch_raw,
                    }

            # ── Aguarda aprovação do plano ─────────────────────────────────────
            # Reset event + decision before publishing the plan so any stale
            # API call that fires between here and the wait() is dropped.
            with _plan_approval_lock:
                _plan_approval_decision = None
            _plan_approval_event.clear()

            _pending_plan_data = plan_dict
            capture.sse_queue.put({"type": "plan_ready", "plan": plan_dict, "num": req_num})

            # Wait for user approval in 30-second ticks, emitting a countdown event
            # each tick so the frontend can show remaining time in the modal.
            # A keepalive inference (1 token) is fired on each tick to prevent
            # LM Studio from auto-unloading the model while the user reviews the plan.
            _APPROVAL_TIMEOUT = 600
            _TICK = 30
            elapsed_wait = 0
            got_response = False
            while elapsed_wait < _APPROVAL_TIMEOUT:
                got_response = _plan_approval_event.wait(timeout=_TICK)
                if got_response:
                    break
                elapsed_wait += _TICK
                remaining = _APPROVAL_TIMEOUT - elapsed_wait
                capture.sse_queue.put({"type": "plan_countdown", "remaining": remaining})
                threading.Thread(target=_lm_keepalive, daemon=True).start()

            if not got_response:
                capture.sse_queue.put({"type": "output", "text": "\nTempo esgotado a aguardar aprovação.\n"})
                entry["status"] = "cancelled"
                return

            with _plan_approval_lock:
                decision = _plan_approval_decision or {"approved": True, "indices": None, "issue_indices": None}
                _plan_approval_decision = None

            approved = decision.get("approved", False) if isinstance(decision, dict) else False
            if not approved:
                feedback_text = decision.get("feedback", "") if isinstance(decision, dict) else ""
                msg = f"\nPlano rejeitado.{(' Feedback: ' + feedback_text) if feedback_text else ''}\n"
                capture.sse_queue.put({"type": "output", "text": msg})
                capture.sse_queue.put({"type": "plan_rejected"})
                entry["status"] = "cancelled"
                return

            # ── Fase 2/3: Developer ────────────────────────────────────────────
            approved_indices: list[int] | None = decision.get("indices") if isinstance(decision, dict) else None
            approved_issue_indices: list[int] | None = decision.get("issue_indices") if isinstance(decision, dict) else None

            # Determine the effective change list after granular filtering
            all_changes = plan_dict.get("changes", [])
            all_issues  = plan_dict.get("issues",  [])
            if approved_indices is None:
                effective_changes = all_changes
            else:
                effective_changes = [all_changes[i] for i in approved_indices if i < len(all_changes)]

            if approved_issue_indices is None:
                effective_issues = all_issues
            else:
                effective_issues = [all_issues[i] for i in approved_issue_indices if i < len(all_issues)]

            # Nothing to implement: skip developer+reviewer and complete
            if not effective_changes and not effective_issues:
                msg = "\n✅ Sem mudanças ou issues a implementar. Tarefa concluída pelo Arquitecto.\n"
                capture.sse_queue.put({"type": "output", "text": msg})
                _last_agent_output = arch_raw
                _last_agent_name = "full-flow"
                entry["output"] = arch_raw
                entry["status"] = "done"
                capture.sse_queue.put({"type": "context_updated", "from_agent": "full-flow", "agent_label": "Fluxo Completo"})
                return  # falls into finally block for cleanup

            parts: list[str] = []
            if approved_indices is not None and len(effective_changes) < len(all_changes):
                parts.append(f"{len(effective_changes)}/{len(all_changes)} mudanças")
            if approved_issue_indices is not None and len(effective_issues) < len(all_issues):
                parts.append(f"{len(effective_issues)}/{len(all_issues)} issues")
            if parts:
                capture.sse_queue.put({"type": "output", "text":
                    f"ℹ️ Aprovação parcial: {', '.join(parts)} seleccionados.\n"})

            # ── Auto-detect: all issues have patch_after → VIA DIRECTA (skip Developer LLM) ──
            effective_creates = [c for c in effective_changes if c.get("action") == "create"]
            patches_ready = (
                bool(effective_issues)
                and not effective_creates
                and all(
                    i.get("snippet", "").strip() and i.get("patch_after", "").strip()
                    for i in effective_issues
                )
            )

            backend_files_applied: list[str] | None = None

            if patches_ready:
                # ── Path sanity check (VIA DIRECTA) — valida issue.file antes de aplicar ──
                _bad_issue_paths = [
                    i.get("file", "")
                    for i in effective_issues
                    if not os.path.isabs(i.get("file", ""))
                    or not os.path.exists(i.get("file", ""))
                ]
                if _bad_issue_paths:
                    capture.sse_queue.put({"type": "output", "text":
                        f"\n⛔ VALIDAÇÃO: {len(_bad_issue_paths)} path(s) de issues não existem no disco:\n"
                        + "\n".join(f"  - {p}" for p in _bad_issue_paths)
                        + "\nO Arquitecto aluciou os caminhos. Corrige o plano ou re-executa.\n"})
                    entry["status"] = "error"
                    return

                # VIA DIRECTA — backend aplica patches directamente, sem Developer LLM
                capture.sse_queue.put({"type": "output", "text":
                    f"\nFase 2/3 — Backend a aplicar {len(effective_issues)} patch(es) directamente"
                    f" (VIA DIRECTA — sem Developer LLM)...\n"})
                files_mod, patch_errors = _apply_patches_from_plan(effective_issues, project_path)
                backend_files_applied = files_mod

                # Build synthetic ImplementationResult for Reviewer context (ANTES/DEPOIS per issue)
                synth_lines = ["## Patches aplicados pelo backend\n"]
                for _iss in effective_issues:
                    if _iss.get("snippet") and _iss.get("patch_after"):
                        _fname = os.path.basename(_iss.get("file", ""))
                        synth_lines.append(f"\n### {_fname}:{_iss.get('line', '?')}")
                        synth_lines.append(f"**{_iss.get('description', '')}**\n")
                        synth_lines.append("**ANTES:**\n```")
                        synth_lines.append(_iss["snippet"])
                        synth_lines.append("```\n**DEPOIS:**\n```")
                        synth_lines.append(_iss["patch_after"])
                        synth_lines.append("```")
                if patch_errors:
                    synth_lines.append(f"\n### Erros ({len(patch_errors)})")
                    synth_lines.extend(f"- ❌ {e}" for e in patch_errors)

                dev_raw = json.dumps({
                    "files_modified": files_mod,
                    "files_created":  [],
                    "syntax_errors":  [],
                    "summary":        "\n".join(synth_lines),
                }, ensure_ascii=False)
                dev_result = None

                if patch_errors and not files_mod:
                    # Todos os patches falharam — não há nada para o Reviewer verificar
                    capture.sse_queue.put({"type": "output", "text":
                        f"\n⛔ {len(patch_errors)} patch(es) falharam e nenhum ficheiro foi alterado."
                        " O Reviewer não será executado.\n"
                        "Causa provável: snippets incorrectos ou paths errados do Arquitecto. Re-executa o pedido.\n"})
                    _last_agent_output = arch_raw
                    _last_agent_name = "full-flow"
                    entry["output"] = arch_raw
                    entry["status"] = "error"
                    return

                if patch_errors:
                    capture.sse_queue.put({"type": "output", "text":
                        f"\n⚠️ {len(patch_errors)} patch(es) falharam — verifica os snippets do Arquitecto.\n"})

            else:
                # VIA LLM — Developer implementa (comportamento actual para features/creates)
                arch_trimmed = _trim_plan_for_developer(arch_raw, approved_indices, approved_issue_indices)

                # ── Path sanity check — catch Architect hallucinations before Developer runs ──
                try:
                    _plan_parsed = json.loads(arch_trimmed)
                    _bad_paths = [
                        c.get("path", "")
                        for c in _plan_parsed.get("changes", [])
                        if not os.path.isabs(c.get("path", ""))
                        or not os.path.exists(c.get("path", ""))
                    ]
                    if _bad_paths:
                        capture.sse_queue.put({"type": "output", "text":
                            f"\n⛔ VALIDAÇÃO: {len(_bad_paths)} path(s) do plano não existem no disco:\n"
                            + "\n".join(f"  - {p}" for p in _bad_paths)
                            + "\nO Arquitecto pode ter alucinado os caminhos. Corrige o plano ou re-executa.\n"})
                        entry["status"] = "error"
                        return
                except Exception:
                    pass  # JSON parse failure handled downstream

                dev_context = arch_trimmed
                _ctx_kb = round(len(arch_trimmed.encode("utf-8")) / 1024, 1)
                capture.sse_queue.put({"type": "output", "text":
                    f"\nFase 2/3 — Developer a implementar... (contexto: {_ctx_kb} KB)\n"})
                _dev_inputs = {"request": user_request, "project_path": project_path, "context": dev_context}
                dev_result, dev_raw = _kickoff_developer(crew.implement_crew, _dev_inputs)
                dev_raw = _validate_and_clean(dev_result, dev_raw, "developer")
                if dev_result is not None:
                    _emit_result_card(dev_result, "developer")

            # ── Fase 3/3: Reviewer ─────────────────────────────────────────────
            capture.sse_queue.put({"type": "output", "text": "\nFase 3/3 — Reviewer a verificar...\n"})
            _rev_inputs = {"request": user_request, "project_path": project_path, "context": dev_raw}
            rev_result = _kickoff_with_retry(
                lambda: crew.review_crew().kickoff(inputs=_rev_inputs),
                capture.sse_queue.put,
            )
            rev_raw = str(rev_result.raw) if hasattr(rev_result, "raw") else str(rev_result)
            # Emit card before validate so the card shows the original reviewer verdict,
            # not the force-approved one that _validate_and_clean may write in place.
            _emit_result_card(rev_result, "reviewer")
            rev_raw = _validate_and_clean(rev_result, rev_raw, "reviewer")

            # ── Ciclo de correcção (se Reviewer rejeitou) ─────────────────────
            fix_result  = None
            rev2_result = None
            final_dev_raw = dev_raw  # last developer output for git add
            rev_pydantic = getattr(rev_result, "pydantic", None)
            if rev_pydantic is not None and not rev_pydantic.approved:
                n_issues = len(rev_pydantic.issues)
                capture.sse_queue.put({
                    "type": "output",
                    "text": f"\nReviewer identificou {n_issues} problema(s) — a corrigir (ciclo 1/1)...\n",
                })
                _fix_inputs = {"request": user_request, "project_path": project_path, "context": rev_raw}
                fix_result, fix_raw = _kickoff_developer(crew.fix_only_crew, _fix_inputs)
                fix_raw = _validate_and_clean(fix_result, fix_raw, "developer")
                if fix_result is not None:
                    _emit_result_card(fix_result, "developer")
                final_dev_raw = fix_raw

                # Re-verificação após correcção
                capture.sse_queue.put({"type": "output", "text": "\nRe-verificação após correções...\n"})
                _rev2_inputs = {"request": user_request, "project_path": project_path, "context": fix_raw}
                rev2_result = _kickoff_with_retry(
                    lambda: crew.review_crew().kickoff(inputs=_rev2_inputs),
                    capture.sse_queue.put,
                )
                rev2_raw = str(rev2_result.raw) if hasattr(rev2_result, "raw") else str(rev2_result)
                _emit_result_card(rev2_result, "reviewer")
                _validate_and_clean(rev2_result, rev2_raw, "reviewer")

                rev2_pydantic = getattr(rev2_result, "pydantic", None)
                if rev2_pydantic is not None and not rev2_pydantic.approved:
                    remaining = len(rev2_pydantic.issues)
                    capture.sse_queue.put({
                        "type": "output",
                        "text": f"\n⚠️ {remaining} problema(s) por resolver após correcção — revê manualmente.\n",
                    })

            _emit_full_flow_summary(arch_result, dev_result, rev_result, fix_result, rev2_result,
                                    backend_files=backend_files_applied)
            _last_agent_output = final_dev_raw
            _last_agent_name = "full-flow"
            entry["output"] = final_dev_raw
            capture.sse_queue.put({"type": "context_updated", "from_agent": "full-flow", "agent_label": "Fluxo Completo"})
            entry["status"] = "done"

        except KeyboardInterrupt:
            capture.sse_queue.put({"type": "output", "text": "\nPedido cancelado.\n"})
            entry["status"] = "cancelled"
        except Exception as e:
            import traceback
            logger.exception("full_flow error (req=%s)", req_num)
            err_str = str(e)
            if "No models loaded" in err_str:
                capture.sse_queue.put({"type": "lm_error", "text":
                    "Nenhum modelo carregado em LM Studio.\n"
                    "Solução: Abre LM Studio → Developer → carrega o modelo e tenta novamente."})
            elif "Invalid response from LLM call - None or empty" in err_str:
                capture.sse_queue.put({"type": "lm_error", "text":
                    "O modelo devolveu resposta vazia — o contexto excedeu a janela do modelo.\n"
                    "Soluções: (1) reduz o número de alterações seleccionadas no plano, "
                    "(2) aumenta 'Context Length' em LM Studio → My Models."})
            elif any(m in err_str or m in type(e).__name__ for m in _CONVERTER_MARKERS):
                capture.sse_queue.put({"type": "lm_error", "text":
                    "O agente atingiu o limite de iterações sem produzir JSON válido.\n"
                    "Soluções: (1) reduz o número de issues aprovados no plano, "
                    "(2) verifica os ficheiros manualmente."})
            elif "Connection error" in err_str or "ConnectError" in err_str or "10051" in err_str or "10061" in err_str:
                capture.sse_queue.put({"type": "lm_error", "text":
                    "LM Studio ficou inacessível durante a execução.\n"
                    "Causas habituais: modelo descarregado por inactividade, LM Studio fechado, ou porta em conflito.\n"
                    "Solução: verifica que LM Studio está a correr e o modelo carregado, depois tenta novamente."})
            else:
                capture.sse_queue.put({"type": "output", "text": f"\nErro: {e}\n{traceback.format_exc()}\n"})
            entry["status"] = "error"
        finally:
            capture._cancel_requested.clear()
            _active_crew_thread = None
            _pending_plan_data = None
            with _plan_approval_lock:
                _plan_approval_decision = None
            entry["elapsed"] = round(time.time() - start)
            with _session_lock:
                session["running"] = False
            _save_session()
            stats = dict(capture._request_tool_stats)
            if stats:
                capture.sse_queue.put({"type": "session_stats", "stats": stats})
            capture._request_tool_stats.clear()
            capture.sse_queue.put({
                "type": "request_done", "num": req_num,
                "status": entry["status"], "elapsed": entry["elapsed"],
                "requests": session["requests"],
            })

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "num": req_num})


@app.route("/api/plan/approve", methods=["POST"])
def plan_approve():
    global _plan_approval_decision
    if _pending_plan_data is None:
        return jsonify({"error": "Nenhum plano pendente de aprovação"}), 400
    data = request.json or {}
    approved_indices       = data.get("approved_indices")        # None = approve all changes
    approved_issue_indices = data.get("approved_issue_indices")  # None = pass all issues
    with _plan_approval_lock:
        _plan_approval_decision = {
            "approved":             True,
            "indices":              approved_indices,
            "issue_indices":        approved_issue_indices,
        }
    _plan_approval_event.set()
    return jsonify({"ok": True})


@app.route("/api/plan/reject", methods=["POST"])
def plan_reject():
    global _plan_approval_decision
    if _pending_plan_data is None:
        return jsonify({"error": "Nenhum plano pendente de aprovação"}), 400
    data = request.json or {}
    feedback = (data.get("feedback") or "").strip()
    with _plan_approval_lock:
        _plan_approval_decision = {"approved": False, "feedback": feedback}
    _plan_approval_event.set()
    return jsonify({"ok": True})


# ── Entry point ───────────────────────────────────────────────────────────────

def run_server(host: str = "127.0.0.1", port: int = 7777, open_browser: bool = False):
    capture.enable()
    print(f"\n🚀  Dev Studio API → http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
