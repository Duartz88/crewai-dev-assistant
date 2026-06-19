"""
Deterministic project scanner — runs as Python code, not as an LLM tool call.

scan_project_sync() is called from app.py BEFORE the crew starts, and the
result is injected into task inputs as {project_map}. This makes the map
unavoidable: it's part of the prompt, not something the LLM can decide to skip.

ProjectScannerTool wraps the same function so agents can trigger a re-scan
when needed (e.g. after implementation to verify what changed).
"""
import fnmatch
import os
import re
from datetime import datetime

from crewai.tools import BaseTool


_SKIP_DIRS = frozenset({
    '.git', '.hg', '.svn',
    'node_modules', '__pycache__', '.venv', 'venv', 'env',
    'dist', 'build', '.angular', '.next', '.nuxt', '.cache',
    'coverage', 'htmlcov', '.mypy_cache', '.pytest_cache',
    '.eggs', 'bin', 'obj', 'Debug', 'Release', 'x64', 'x86',
    'packages', 'Packages', '.vs', '.idea',
    'migrations', '__MACOSX', 'wwwroot',
})

_CODE_EXTS = frozenset({
    '.py', '.ts', '.tsx', '.js', '.jsx',
    '.cs', '.java', '.go', '.rs', '.php', '.rb',
    '.html', '.vue', '.svelte',
    '.yaml', '.yml', '.json', '.toml',
    '.sql', '.ps1', '.psm1', '.psd1', '.sh',
})

_SKIP_FILES = frozenset({
    'package-lock.json', 'yarn.lock', 'uv.lock', 'poetry.lock',
    'Pipfile.lock', 'Cargo.lock', 'composer.lock', 'Gemfile.lock',
})

_MAX_FILES_PER_DIR   = 30
_MAX_TOTAL_FILES     = 200
_MAX_ROUTES          = 120
_MAX_CLASSES         = 150
_LARGE_FILE_LINES    = 500   # files above this get a ⚠️ annotation in the PROJECT MAP

# Class / type extraction per extension
_CLASS_RE: dict[str, re.Pattern] = {
    '.py':   re.compile(r'^class\s+(\w+)'),
    '.ts':   re.compile(r'^(?:export\s+(?:abstract\s+|default\s+)?)?class\s+(\w+)'),
    '.tsx':  re.compile(r'^(?:export\s+(?:default\s+)?)?(?:class|function)\s+(\w+)'),
    '.cs':   re.compile(r'(?:public|private|internal|protected)\s+(?:abstract\s+|sealed\s+|static\s+)?class\s+(\w+)'),
    '.java': re.compile(r'(?:public\s+)?(?:abstract\s+)?class\s+(\w+)'),
    '.go':   re.compile(r'^type\s+(\w+)\s+struct'),
}

_SKIP_CLASS_NAMES = frozenset({
    'Meta', 'Config', 'Base', 'Abstract', 'Mixin', 'Model', 'Schema',
    'View', 'Test', 'Exception', 'Error', 'Module', 'App',
})


def _load_crewignore(project_path: str) -> list[str]:
    """Load glob patterns from .crewignore in the project root."""
    path = os.path.join(project_path, '.crewignore')
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8', errors='ignore') as fh:
            return [
                ln.strip() for ln in fh
                if ln.strip() and not ln.startswith('#')
            ]
    except OSError:
        return []


def _is_crewignored(rel_path: str, patterns: list[str]) -> bool:
    """Return True if rel_path matches any .crewignore pattern."""
    name = os.path.basename(rel_path)
    for p in patterns:
        if fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel_path, p):
            return True
    return False


def scan_project_sync(project_path: str) -> str:
    """
    Walk the project tree and produce a compact PROJECT MAP.
    Called from app.py — no LLM involved, always runs before any crew kickoff.
    """
    if not project_path or not os.path.isdir(project_path):
        return f"[PROJECT MAP indisponível: caminho não encontrado — {project_path}]"

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    files:   list[tuple[str, int]]          = []  # (rel_path, line_count)
    routes:  list[tuple[str, str, str]]     = []  # (METHOD, /path, rel_path:line)
    classes: list[tuple[str, str, int]]     = []  # (ClassName, rel_path, line_num)

    ignore_patterns = _load_crewignore(project_path)

    for root, dirs, fnames in os.walk(project_path):
        dirs[:] = sorted(
            d for d in dirs
            if d not in _SKIP_DIRS and not d.startswith('.')
        )

        for fname in sorted(fnames):
            if fname in _SKIP_FILES:
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _CODE_EXTS:
                continue

            abs_path = os.path.join(root, fname)
            rel_path = os.path.relpath(abs_path, project_path).replace('\\', '/')

            if _is_crewignored(rel_path, ignore_patterns):
                continue

            try:
                with open(abs_path, encoding='utf-8', errors='ignore') as fh:
                    content_lines = fh.readlines()
            except OSError:
                continue

            line_count = len(content_lines)
            if len(files) < _MAX_TOTAL_FILES:
                files.append((rel_path, line_count))

            _extract_routes(content_lines, rel_path, ext, routes)
            _extract_classes(content_lines, rel_path, ext, classes)

    out: list[str] = [
        f"╔══════════════════════════════════════════════════════════╗",
        f"  PROJECT MAP — {os.path.basename(project_path)}  |  {ts}",
        f"╚══════════════════════════════════════════════════════════╝",
        "",
    ]

    # ── File tree ────────────────────────────────────────────────────────────────
    total = len(files)
    cap_note = f"  (mostrando {total}; limite={_MAX_TOTAL_FILES})" if total >= _MAX_TOTAL_FILES else ""
    out.append(f"📁 FICHEIROS{cap_note}")
    _format_tree(files, out)
    out.append("")

    # ── Routes ───────────────────────────────────────────────────────────────────
    if routes:
        out.append(f"🔀 ROTAS DETECTADAS ({len(routes)})")
        shown = routes[:_MAX_ROUTES]
        for method, path, loc in sorted(shown, key=lambda r: r[1]):
            pad = " " * max(0, 7 - len(method))
            out.append(f"  {method}{pad} {path:<42s}  → {loc}")
        if len(routes) > _MAX_ROUTES:
            out.append(f"  … e mais {len(routes) - _MAX_ROUTES} rotas não mostradas")
        out.append("")

    # ── Classes ──────────────────────────────────────────────────────────────────
    if classes:
        out.append(f"🏛️  CLASSES / SERVIÇOS / COMPONENTES ({len(classes)})")
        shown_c = classes[:_MAX_CLASSES]
        for name, rel_file, lineno in sorted(shown_c, key=lambda c: c[0].lower()):
            out.append(f"  {name:<42s}  → {rel_file}:{lineno}")
        if len(classes) > _MAX_CLASSES:
            out.append(f"  … e mais {len(classes) - _MAX_CLASSES} classes não mostradas")
        out.append("")

    out += [
        "⚠️  REGRAS DO PROJECT MAP:",
        "  • Ficheiros marcados com ⚠️ GRANDE (>500 ln): NÃO leres a menos que o pedido mencione",
        "    esse ficheiro EXPLICITAMENTE pelo nome. Ler ficheiros grandes desnecessários esgota",
        "    a janela de contexto e causa falhas. Ignora-os completamente se não forem relevantes.",
        "  • Para excluir ficheiros permanentemente do PROJECT MAP, cria .crewignore na raiz do",
        "    projecto (um padrão por linha, ex: GlinttSetupPosto.ps1 ou *.bak).",
        "  • Antes de criar uma rota, verifica que NÃO está na lista acima.",
        "  • Antes de criar uma classe/service, verifica que NÃO está na lista acima.",
        "  • Se uma rota ou classe não consta aqui, provavelmente não existe.",
        "  • Os números de linha são aproximados — usa read_file para confirmar o código exacto.",
    ]

    return "\n".join(out)


# ── Route extraction ──────────────────────────────────────────────────────────────

def _extract_routes(
    lines: list[str], rel_path: str, ext: str,
    out: list[tuple[str, str, str]],
) -> None:
    for i, raw in enumerate(lines, 1):
        line = raw.strip()

        if ext == '.py':
            # @router.get("/path") / @app.post("/path")
            m = re.match(
                r'@(?:router|app|bp|blueprint|api|v\d+_router)\.'
                r'(get|post|put|delete|patch|head|options)\s*\(\s*["\']([^"\']+)',
                line, re.I,
            )
            if m:
                out.append((m.group(1).upper(), m.group(2), f"{rel_path}:{i}"))
                continue
            # @app.route("/path", methods=["GET", "POST"])
            m = re.match(r'@(?:app|bp|blueprint)\.route\s*\(\s*["\']([^"\']+)', line, re.I)
            if m:
                path = m.group(1)
                mm = re.search(r'methods\s*=\s*\[([^\]]+)\]', line)
                if mm:
                    for verb in mm.group(1).replace('"', '').replace("'", '').split(','):
                        out.append((verb.strip().upper(), path, f"{rel_path}:{i}"))
                else:
                    out.append(('GET', path, f"{rel_path}:{i}"))

        elif ext in ('.ts', '.js', '.tsx', '.jsx'):
            # Express: router.get('/path', ...) or app.post('/path', ...)
            m = re.match(
                r'(?:this\.)?(?:router|app|server)\.'
                r'(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)',
                line, re.I,
            )
            if m:
                out.append((m.group(1).upper(), m.group(2), f"{rel_path}:{i}"))

        elif ext == '.cs':
            # [HttpGet("path")] / [HttpPost]
            m = re.match(r'\[Http(Get|Post|Put|Delete|Patch)\s*(?:\(\s*"([^"]+)")?', line, re.I)
            if m:
                path = m.group(2) or '(ver Route base)'
                out.append((m.group(1).upper(), path, f"{rel_path}:{i}"))
            # [Route("path")]
            m = re.match(r'\[Route\s*\(\s*"([^"]+)"', line, re.I)
            if m:
                out.append(('ROUTE', m.group(1), f"{rel_path}:{i}"))


# ── Class extraction ──────────────────────────────────────────────────────────────

def _extract_classes(
    lines: list[str], rel_path: str, ext: str,
    out: list[tuple[str, str, int]],
) -> None:
    pattern = _CLASS_RE.get(ext)
    if not pattern:
        return
    for i, raw in enumerate(lines, 1):
        m = pattern.match(raw.strip())
        if m:
            name = m.group(1)
            if len(name) < 3 or name in _SKIP_CLASS_NAMES:
                continue
            out.append((name, rel_path, i))


# ── Tree formatter ────────────────────────────────────────────────────────────────

def _format_tree(files: list[tuple[str, int]], out: list[str]) -> None:
    by_dir: dict[str, list[tuple[str, int]]] = {}
    for rel_path, lc in files:
        parts = rel_path.split('/')
        top = parts[0] if len(parts) > 1 else '__root__'
        by_dir.setdefault(top, []).append((rel_path, lc))

    for top, flist in sorted(by_dir.items()):
        label = '' if top == '__root__' else top + '/'
        if label:
            out.append(f"  {label}")
        for rel_path, lc in flist[:_MAX_FILES_PER_DIR]:
            prefix = '    ' if label else '  '
            size_tag = f"  ⚠️ GRANDE ({lc} ln)" if lc > _LARGE_FILE_LINES else f"  ({lc} ln)"
            out.append(f"{prefix}{rel_path}{size_tag}")
        if len(flist) > _MAX_FILES_PER_DIR:
            out.append(f"    … e mais {len(flist) - _MAX_FILES_PER_DIR} ficheiros")


# ── Tool wrapper ──────────────────────────────────────────────────────────────────

class ProjectScannerTool(BaseTool):
    """Re-scan the project on demand. Primary scan runs from app.py before crew start."""
    name: str = "scan_project"
    description: str = (
        "Re-escaneia o projeto e devolve o PROJECT MAP actualizado com todos os ficheiros, "
        "rotas e classes detectadas. Usa quando precisas de confirmar o estado actual do projeto "
        "após alterações, ou quando o PROJECT MAP inicial pode estar desactualizado."
    )
    project_path: str = ""

    def _run(self, query: str = "") -> str:  # type: ignore[override]
        return scan_project_sync(self.project_path)
