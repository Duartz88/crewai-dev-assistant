"""
Intercepts sys.stdout and builtins.input so crew output and approval
prompts can be streamed to the browser via SSE.
"""
import builtins
import queue
import re
import sys
import threading

sse_queue: queue.Queue = queue.Queue()
input_prompt_event = threading.Event()
input_response_queue: queue.Queue = queue.Queue(maxsize=1)
_cancel_requested = threading.Event()

_current_prompt: str = ""
_dashboard_active = False
_original_input = builtins.input
_output_buffer: list[str] = []
_BUFFER_SIZE = 80

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# ── Box title suppression ──────────────────────────────────────────────────────
_BOX_UUID  = re.compile(r'^ID:\s+[0-9a-f\-]{36}$', re.I)
_BOX_META  = re.compile(r'^(Name|Agent Name|Task Name|Crew Name|Tool|Output):\s+', re.I)

# Box titles whose content should be skipped entirely (prefix-matched).
_BOX_SKIP_PREFIXES = (
    "crew execution started",
    "crew execution completed",
    "crew completion",
    "task started",
    "task completion",
    "task completed",
    "tool execution completed",
    "agent started",
    "agent final answer",   # suppress duplicate of final answer already in output
    "tracing status",
)

# Plain-text lines emitted by CrewAI's event bus (not in Rich boxes) that
# duplicate information already shown by other means.
_PLAIN_SKIP = frozenset({
    "crew execution started", "crew execution completed", "crew completion",
    "task started", "task completion", "task completed",
    "agent started", "agent final answer", "tracing status",
    # native tool event lines (appear as plain text alongside Rich boxes)
    "tool execution started", "tool execution completed", "tool completed",
    "tool completion",
})

# Regex patterns for plain-text noise lines that can't be matched exactly
_PLAIN_SKIP_RE = re.compile(
    r'^('
    r'\[Finalize\]\s'           # "[Finalize] todos_count=..." internal CrewAI line
    r')',
    re.IGNORECASE,
)

# Tools whose file content is suppressed from the SSE output stream.
# A compact tool card with path is shown instead.
_SILENT_OBS_TOOLS = frozenset({
    "read_file",
    "list_project_structure",
    "read_project_memory",
})


def _should_skip_box(title: str) -> bool:
    t = title.strip().lower()
    return any(t == p or t.startswith(p + " ") or t.startswith(p + "(") for p in _BOX_SKIP_PREFIXES)


# ── Native tool tracking (function-calling / non-ReAct style) ─────────────────
_native_tool_args: dict[str, str] = {}      # tool_name → last raw args string
_native_pending: list[dict] = []            # events queued inside _clean_rich_box

_FILE_PATH_RE = re.compile(r"file_path['\"\s:]+([^'\"\}\n,]+)")


def _extract_path(args: str) -> str:
    """Pull a file path out of a raw Args string like {'file_path': 'D:/...'}."""
    m = _FILE_PATH_RE.search(args)
    return m.group(1).strip("'\"/ \\").replace("\\\\", "\\") if m else args[:200]


# ── Rich box → clean text ──────────────────────────────────────────────────────
# Box parsing state is MODULE-LEVEL so it persists across write() calls.
# If a Rich panel is split across two write() calls (first call ends mid-box),
# the state is preserved correctly for the second call.
_box: dict = {
    "in_skip":       False,   # currently inside a skip-listed box
    "in_tool_start": False,   # currently inside a "Tool Execution Started" box
    "last_title":    "",      # lowercase title of current box
    "ts_info":       {"name": "", "args": ""},  # scratch for tool-start parsing
}


def _clean_rich_box(text: str) -> str:
    """Convert Rich panel box-drawing chars to clean text for SSE.

    Box parsing state persists across calls via the module-level _box dict,
    so boxes split across write() calls are handled correctly.

    Special cases:
    • "Tool Execution Started" boxes → suppressed, tool name + args queued
      as SSE events in _native_pending.
    • Skip-listed boxes → suppressed entirely.
    • Regular boxes → title kept as a plain separator line.
    """
    global _native_pending

    out: list[str] = []
    prev_blank = False

    for raw in text.split('\n'):
        s = raw.strip()

        # ── Top border: ╭──── Title ────╮ ─────────────────────────────────
        if s.startswith('╭'):
            title = re.sub(r'[╭╮─]', '', s).strip()
            _box["last_title"] = title.lower()
            _box["in_tool_start"] = _box["last_title"].startswith("tool execution started")

            if _box["in_tool_start"]:
                _box["in_skip"] = True
                _box["ts_info"] = {"name": "", "args": ""}
            elif title and _should_skip_box(title):
                _box["in_skip"] = True
                _box["in_tool_start"] = False
            else:
                _box["in_skip"] = False
                _box["in_tool_start"] = False
                if title:
                    out.append(f'──── {title} ────')
            prev_blank = False
            continue

        # ── Bottom border: ╰──────╯ ───────────────────────────────────────
        if s.startswith('╰'):
            if _box["in_tool_start"] and _box["ts_info"]["name"]:
                name = _box["ts_info"]["name"]
                args = _box["ts_info"]["args"]
                _native_tool_args[name] = args
                _native_pending.append({"type": "tool_call",  "tool": name})
                if args:
                    _native_pending.append({
                        "type": "tool_input", "tool": name,
                        "input": _extract_path(args) if name in _SILENT_OBS_TOOLS else args[:400],
                    })
            _box["in_skip"] = False
            _box["in_tool_start"] = False
            continue

        # ── Content line: │  text  │ ──────────────────────────────────────
        if s.startswith('│'):
            content = s.lstrip('│').rstrip('│').strip()
            if not content:
                continue
            if _box["in_tool_start"]:
                if content.startswith('Tool: '):
                    _box["ts_info"]["name"] = content[6:].strip()
                elif content.startswith('Args: '):
                    _box["ts_info"]["args"] = content[6:].strip()
                continue
            if _box["in_skip"]:
                continue
            if content.lower() == _box["last_title"]:
                continue
            if _BOX_UUID.match(content) or _BOX_META.match(content):
                continue
            out.append(content)
            prev_blank = False
            continue

        # ── Regular line ───────────────────────────────────────────────────
        if not s:
            if not prev_blank:
                out.append('')
                prev_blank = True
        else:
            sl = s.lower()
            if sl in _PLAIN_SKIP:
                continue
            # Check prefix-based noise patterns (e.g. "[Finalize] ...")
            # Also suppress numbered variants: "Tool Execution Started (#1)"
            if _PLAIN_SKIP_RE.match(s):
                continue
            if any(sl.startswith(p) for p in ("tool execution started", "tool execution completed")):
                continue
            out.append(raw)
            prev_blank = False

    return '\n'.join(out)


# ── ReAct tool event detection ─────────────────────────────────────────────────
_TOOL_RE  = re.compile(r'^\s*Action:\s*(\S.*)', re.IGNORECASE)
_INPUT_RE = re.compile(r'^\s*Action Input:\s*(.*)', re.IGNORECASE)
_OBS_RE   = re.compile(r'^\s*Observation:\s*(.*)', re.IGNORECASE)
_END_OBS  = re.compile(r'^\s*(Thought:|Action:|Final Answer:)', re.IGNORECASE)

_tool_ctx: dict = {"tool": "", "obs": [], "in_obs": False}


def _parse_tool_events(lines: list[str]) -> list[dict]:
    """Parse CrewAI ReAct output lines and emit structured tool events."""
    ctx = _tool_ctx
    events: list[dict] = []

    for line in lines:
        s = line.strip()
        if not s:
            continue

        if ctx["in_obs"]:
            if _END_OBS.match(s) or _TOOL_RE.match(s):
                if ctx["obs"]:
                    events.append({
                        "type": "tool_result",
                        "tool": ctx["tool"],
                        "output": "\n".join(ctx["obs"])[:1000],
                    })
                ctx["in_obs"] = False
                ctx["obs"] = []
            else:
                ctx["obs"].append(s)
                continue

        m = _TOOL_RE.match(s)
        if m:
            ctx["tool"] = m.group(1).strip()
            ctx["in_obs"] = False
            ctx["obs"] = []
            events.append({"type": "tool_call", "tool": ctx["tool"]})
            continue

        m = _INPUT_RE.match(s)
        if m and ctx["tool"]:
            events.append({"type": "tool_input", "tool": ctx["tool"], "input": m.group(1).strip()[:600]})
            continue

        m = _OBS_RE.match(s)
        if m and ctx["tool"]:
            ctx["in_obs"] = True
            first = m.group(1).strip()
            ctx["obs"] = [first] if first else []
            continue

    return events


# ── Native tool result line (function-calling style) ──────────────────────────
# Pattern: "Tool read_file executed with result: <content>"
# Emitted as plain text by CrewAI's event bus (not via Rich boxes).
_NATIVE_RESULT_RE = re.compile(
    r'^Tool (\S+) executed with result:\s*(.*)',
    re.IGNORECASE | re.DOTALL,
)


def _handle_native_result(line: str) -> bool:
    """Return True (and emit SSE) if line is a native tool result that should
    be suppressed or replaced.  Called on each clean line before normal emit."""
    m = _NATIVE_RESULT_RE.match(line.strip())
    if not m:
        return False
    tool_name = m.group(1)
    if tool_name not in _SILENT_OBS_TOOLS:
        return False

    # Emit compact indicator instead of content
    path = _native_tool_args.get(tool_name, "")
    display = _extract_path(path) if path else tool_name
    sse_queue.put({"type": "tool_call",  "tool": tool_name})
    sse_queue.put({"type": "tool_input", "tool": tool_name, "input": display})
    return True  # caller must NOT emit the original line


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


# ── stdout capture ─────────────────────────────────────────────────────────────

class _TeeOutput:
    def __init__(self, original):
        self._orig = original

    def write(self, text: str):
        if _cancel_requested.is_set():
            _cancel_requested.clear()
            raise KeyboardInterrupt("Pedido cancelado pelo utilizador")

        self._orig.write(text)

        clean = _clean_rich_box(strip_ansi(text))

        # Flush events gathered inside _clean_rich_box (tool-start boxes)
        global _native_pending
        if _native_pending:
            for ev in _native_pending:
                sse_queue.put(ev)
            _native_pending.clear()

        if not clean.strip():
            return

        lines = clean.splitlines()
        non_empty = [l for l in lines if l.strip()]

        # Check for native tool result lines (function-calling path)
        # If ANY line is a silent native result, suppress the whole write
        for line in non_empty:
            if _handle_native_result(line):
                return  # content suppressed; tool card already emitted

        # Snapshot ReAct obs state BEFORE parsing
        pre_silent = _tool_ctx["in_obs"] and _tool_ctx["tool"] in _SILENT_OBS_TOOLS

        if non_empty:
            _output_buffer.extend(non_empty)
            while len(_output_buffer) > _BUFFER_SIZE:
                _output_buffer.pop(0)
            for event in _parse_tool_events(non_empty):
                sse_queue.put(event)

        post_silent = _tool_ctx["in_obs"] and _tool_ctx["tool"] in _SILENT_OBS_TOOLS

        if pre_silent:
            return

        if post_silent:
            pre_obs: list[str] = []
            for line in lines:
                if _OBS_RE.match(line.strip()):
                    break
                pre_obs.append(line)
            if pre_obs and any(l.strip() for l in pre_obs):
                sse_queue.put({"type": "output", "text": '\n'.join(pre_obs)})
            return

        sse_queue.put({"type": "output", "text": clean})

    def flush(self):
        self._orig.flush()

    def fileno(self):
        return self._orig.fileno()

    def isatty(self):
        return self._orig.isatty()


# ── input() intercept ─────────────────────────────────────────────────────────

def _is_crewai_human_input(prompt: str) -> bool:
    return not prompt.strip()


def _dashboard_input(prompt: str = "") -> str:
    global _current_prompt
    clean_prompt = strip_ansi(str(prompt))

    if _is_crewai_human_input(clean_prompt):
        sse_queue.put({"type": "output", "text": "💬 [auto] CrewAI human_input aceite automaticamente"})
        return ""

    _current_prompt = clean_prompt
    context = "\n".join(_output_buffer[-40:])
    sse_queue.put({"type": "input_needed", "prompt": clean_prompt, "context": context})
    input_prompt_event.set()
    try:
        response = input_response_queue.get(timeout=600)
    except queue.Empty:
        response = ""
    sse_queue.put({"type": "output", "text": f"{clean_prompt}{response}"})
    return response


# ── Enable / disable ──────────────────────────────────────────────────────────

def enable():
    global _dashboard_active
    if _dashboard_active:
        return
    sys.stdout = _TeeOutput(sys.__stdout__)
    builtins.input = _dashboard_input
    _dashboard_active = True


def disable():
    global _dashboard_active
    if not _dashboard_active:
        return
    sys.stdout = sys.__stdout__
    builtins.input = _original_input
    _dashboard_active = False
