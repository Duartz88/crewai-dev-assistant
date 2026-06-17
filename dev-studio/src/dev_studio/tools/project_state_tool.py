"""
Shared project state — deterministic scanner + persistent JSON index.

Scans Python, TypeScript/TSX, and PowerShell files and writes
.crew_project_state.json to the project root.  Agents read this
via ProjectStateReadTool instead of re-reading every file.

What is indexed:
  routes     — API endpoints (FastAPI / Flask decorators)
  services   — Angular @Injectable classes + Python service classes
  models     — Pydantic BaseModel / SQLAlchemy Base / TypeScript interfaces
  components — Angular @Component classes with selector
  files      — every scanned file with its extracted symbols
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

STATE_FILENAME = ".crew_project_state.json"

_SKIP_DIRS = frozenset({
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".angular", "coverage", ".mypy_cache", ".pytest_cache",
    "obj", "bin", ".vs",
})


# ── Python scanner ────────────────────────────────────────────────────────────

_PY_ROUTE_FAST = re.compile(
    r'@\w+\.(get|post|put|delete|patch|options|head)\s*\(\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_PY_ROUTE_FLASK = re.compile(
    r'@\w+\.route\s*\(\s*["\']([^"\']+)["\'](?:.*?methods\s*=\s*\[([^\]]+)\])?',
    re.IGNORECASE,
)
_PY_CLASS = re.compile(r'^class\s+(\w+)\s*(?:\(([^)]*)\))?', re.MULTILINE)
_PY_DEF   = re.compile(r'^(?:async\s+)?def\s+([a-zA-Z]\w*)\s*\(', re.MULTILINE)
_PY_BASE_MODELS = re.compile(r'\b(BaseModel|Base|Schema|DeclarativeBase)\b')


def _scan_python(path: str) -> dict:
    info: dict = {"type": "python", "routes": [], "classes": [], "models": [], "functions": []}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        content = "".join(lines)
    except Exception:
        return info

    for i, line in enumerate(lines, 1):
        m = _PY_ROUTE_FAST.search(line)
        if m:
            info["routes"].append({"method": m.group(1).upper(), "path": m.group(2), "line": i})
            continue
        m2 = _PY_ROUTE_FLASK.search(line)
        if m2:
            path_val = m2.group(1)
            raw_methods = m2.group(2) or "GET"
            for meth in re.findall(r"['\"](\w+)['\"]", raw_methods):
                info["routes"].append({"method": meth.upper(), "path": path_val, "line": i})

    for m in _PY_CLASS.finditer(content):
        name = m.group(1)
        bases = m.group(2) or ""
        entry: dict = {"name": name}
        if _PY_BASE_MODELS.search(bases):
            entry["is_model"] = True
            info["models"].append({"name": name})
        info["classes"].append(entry)

    info["functions"] = [
        m.group(1) for m in _PY_DEF.finditer(content)
        if not m.group(1).startswith("_")
    ]
    return info


# ── TypeScript / TSX scanner ──────────────────────────────────────────────────

_TS_CLASS    = re.compile(r'\bclass\s+(\w+)')
_TS_SELECTOR = re.compile(r"selector\s*:\s*['\"]([^'\"]+)['\"]")
_TS_IFACE    = re.compile(r'\binterface\s+(\w+)')
_TS_METHOD   = re.compile(r'^\s{2,}(?:async\s+)?([a-zA-Z]\w*)\s*\([^)]*\)\s*(?::\s*\S+)?\s*\{', re.MULTILINE)
_TS_HTTP     = re.compile(r'this\.(?:http|_http)\.\w+\s*\(\s*[`\'"]([^`\'"]+)[`\'"]', re.IGNORECASE)
_TS_COMP     = re.compile(r'@Component\b')
_TS_SVC      = re.compile(r'@Injectable\b')


def _scan_typescript(path: str) -> dict:
    info: dict = {"type": "typescript", "classes": [], "interfaces": [], "http_calls": [], "methods": []}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return info

    is_comp = bool(_TS_COMP.search(content))
    is_svc  = bool(_TS_SVC.search(content))
    sel_m   = _TS_SELECTOR.search(content)
    selector = sel_m.group(1) if sel_m else ""

    for m in _TS_CLASS.finditer(content):
        info["classes"].append({
            "name": m.group(1),
            "is_component": is_comp,
            "is_service": is_svc,
            "selector": selector if is_comp else "",
        })

    info["interfaces"] = [m.group(1) for m in _TS_IFACE.finditer(content)]
    info["http_calls"]  = list(dict.fromkeys(m.group(1) for m in _TS_HTTP.finditer(content)))
    info["methods"]     = [
        m.group(1) for m in _TS_METHOD.finditer(content)
        if not m.group(1).startswith("_") and m.group(1) not in ("if", "for", "while", "switch", "catch")
    ]
    return info


# ── PowerShell scanner ────────────────────────────────────────────────────────

_PS_FUNC = re.compile(r'^function\s+([A-Za-z][\w-]*)', re.MULTILINE | re.IGNORECASE)


def _scan_powershell(path: str) -> dict:
    info: dict = {"type": "powershell", "functions": []}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return info
    info["functions"] = [m.group(1) for m in _PS_FUNC.finditer(content)]
    return info


# ── Dispatch table ────────────────────────────────────────────────────────────

_SCANNERS: dict[str, object] = {
    ".py":   _scan_python,
    ".ts":   _scan_typescript,
    ".tsx":  _scan_typescript,
    ".ps1":  _scan_powershell,
    ".psm1": _scan_powershell,
}


# ── Core scan function ────────────────────────────────────────────────────────

def scan_project_state(project_path: str) -> dict:
    """Walk project tree and return structured state dict."""
    state: dict = {
        "project_path": project_path,
        "scanned_at": datetime.now().isoformat(),
        "files": {},
        "routes": {},
        "services": {},
        "models": {},
        "components": {},
    }

    for root, dirs, files in os.walk(project_path):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for fname in sorted(files):
            ext = os.path.splitext(fname)[1].lower()
            scanner = _SCANNERS.get(ext)
            if scanner is None:
                continue
            abs_path = os.path.join(root, fname)
            rel_path = os.path.relpath(abs_path, project_path).replace("\\", "/")

            try:
                info = scanner(abs_path)  # type: ignore[call-arg]
            except Exception:
                continue

            state["files"][rel_path] = info

            # Aggregate routes
            for route in info.get("routes", []):
                key = f"{route['method']} {route['path']}"
                state["routes"][key] = {"file": rel_path, "line": route.get("line", 0)}

            # Aggregate models (Python)
            for model in info.get("models", []):
                state["models"][model["name"]] = {"file": rel_path}

            # Aggregate TypeScript components and services
            for cls in info.get("classes", []):
                if info.get("type") == "typescript":
                    if cls.get("is_component"):
                        state["components"][cls["name"]] = {
                            "file": rel_path,
                            "selector": cls.get("selector", ""),
                        }
                    elif cls.get("is_service"):
                        state["services"][cls["name"]] = {
                            "file": rel_path,
                            "http_calls": info.get("http_calls", []),
                        }

    return state


def write_project_state(project_path: str) -> dict:
    """Scan project and persist .crew_project_state.json. Returns the state."""
    state = scan_project_state(project_path)
    out_path = os.path.join(project_path, STATE_FILENAME)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return state


def read_project_state(project_path: str) -> dict | None:
    """Load existing .crew_project_state.json, or None if absent/corrupt."""
    out_path = os.path.join(project_path, STATE_FILENAME)
    if not os.path.exists(out_path):
        return None
    try:
        with open(out_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def summarize_state(state: dict) -> str:
    """One-line summary for SSE display."""
    return (
        f"Project State indexado: {len(state['files'])} ficheiros"
        f" | {len(state['routes'])} rotas"
        f" | {len(state['services'])} serviços"
        f" | {len(state['models'])} modelos"
        f" | {len(state['components'])} componentes"
    )


# ── CrewAI tool ───────────────────────────────────────────────────────────────

class ProjectStateReadInput(BaseModel):
    query: str = Field(
        default="all",
        description=(
            "Section to retrieve: 'routes' (all API endpoints), 'services' (Angular/Python services), "
            "'models' (Pydantic/SQLAlchemy models), 'components' (Angular components), "
            "'files' (all indexed files with symbols), or 'all' (complete state)."
        ),
    )


class ProjectStateReadTool(BaseTool):
    name: str = "read_project_state"
    description: str = (
        "Read the shared project state index — structured JSON with all routes, services, "
        "models, and components found in the project. "
        "Use query='routes' to see all API endpoints with file and line. "
        "Use query='services' for Angular services and Python service classes. "
        "Use query='models' for Pydantic/SQLAlchemy/TypeScript models. "
        "Use query='components' for Angular components with selectors. "
        "Use query='files' for all scanned files with extracted symbols. "
        "This is the source of truth — use it before proposing any route or service. "
        "If a route is not here, it does not exist."
    )
    args_schema: type[BaseModel] = ProjectStateReadInput
    _project_path: str = ""

    def __init__(self, project_path: str = "", **kwargs: object) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_project_path", project_path)

    def _run(self, query: str = "all") -> str:
        state = read_project_state(self._project_path)
        if state is None:
            return (
                "⚠️ Project state não encontrado (.crew_project_state.json ausente). "
                "O scanner corre automaticamente antes de cada pedido."
            )

        q = query.strip().lower()
        valid = {"routes", "services", "models", "components", "files", "all"}
        if q not in valid:
            return f"Query '{query}' inválida. Usa: {', '.join(sorted(valid))}."

        if q == "all":
            data = {k: v for k, v in state.items() if k != "files"}
            data["file_count"] = len(state.get("files", {}))
            data["scanned_at"] = state.get("scanned_at", "")
        else:
            data = {q: state.get(q, {}), "scanned_at": state.get("scanned_at", "")}

        return json.dumps(data, ensure_ascii=False, indent=2)
