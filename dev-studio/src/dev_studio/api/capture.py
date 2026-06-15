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

# ── Rich box-drawing cleanup ───────────────────────────────────────────────────
_BOX_UUID = re.compile(r'^ID:\s+[0-9a-f\-]{36}$', re.I)
_BOX_META = re.compile(r'^(Name|Agent Name|Task Name|Crew Name):\s+', re.I)

# Box titles whose entire box should be silently discarded (prefix-matched).
# Prefix matching handles suffixes like "(#7)" in "Tool Execution Started (#7)".
_BOX_SKIP_PREFIXES = (
    "crew execution started",
    "crew execution completed",
    "task started",
    "task completion",
    "task completed",
    "tool execution started",
    "tool execution completed",
    "agent started",
    "tracing status",
)


def _should_skip_box(title: str) -> bool:
    t = title.strip().lower()
    return any(t == p or t.startswith(p + " ") or t.startswith(p + "(") for p in _BOX_SKIP_PREFIXES)


# Tools whose raw Observation content is suppressed from the SSE output stream.
# The tool card (tool_call / tool_input events) already shows name + path.
_SILENT_OBS_TOOLS = frozenset({
    "read_file",
    "list_project_structure",
    "read_project_memory",
})


def _clean_rich_box(text: str) -> str:
    """Convert Rich panel box-drawing chars to clean text for SSE.

    ╭──── Title ────╮  →  ──── Title ──── (or skipped if noisy)
    │  content  │       →  content (or skipped if metadata/UUID)
    ╰────────────╯      →  (always skipped)
    """
    out: list[str] = []
    prev_blank = False
    last_title = ''
    in_skip_box = False  # True while inside a box whose content we want to drop

    for raw in text.split('\n'):
        s = raw.strip()

        # Top border: ╭──── Title ────╮
        if s.startswith('╭'):
            title = re.sub(r'[╭╮─]', '', s).strip()
            last_title = title.lower()
            if title and _should_skip_box(title):
                in_skip_box = True
            else:
                in_skip_box = False
                if title:
                    out.append(f'──── {title} ────')
            prev_blank = False
            continue

        # Bottom border: ╰──────╯ → skip, reset skip-box flag
        if s.startswith('╰'):
            in_skip_box = False
            continue

        # Content line: │  text  │ — skip entirely if inside a noisy box
        if s.startswith('│'):
            if in_skip_box:
                continue
            content = s.lstrip('│').rstrip('│').strip()
            if not content:
                continue  # blank box padding → discard
            if content.lower() == last_title:
                continue  # duplicate of the title we already emitted
            if _BOX_UUID.match(content) or _BOX_META.match(content):
                continue
            out.append(content)
            prev_blank = False
            continue

        # Regular line
        if not s:
            if not prev_blank:
                out.append('')
                prev_blank = True
        else:
            out.append(raw)
            prev_blank = False

    return '\n'.join(out)


# ── Tool event detection ───────────────────────────────────────────────────────
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
                # fall through — re-evaluate this line below
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


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


class _TeeOutput:
    def __init__(self, original):
        self._orig = original

    def write(self, text: str):
        # Check cancellation before doing anything
        if _cancel_requested.is_set():
            _cancel_requested.clear()
            raise KeyboardInterrupt("Pedido cancelado pelo utilizador")

        self._orig.write(text)
        clean = _clean_rich_box(strip_ansi(text))
        if not clean.strip():
            return

        lines = clean.splitlines()
        non_empty = [l for l in lines if l.strip()]

        # Snapshot obs state BEFORE parsing (tells us if we were already in silent obs)
        pre_silent = _tool_ctx["in_obs"] and _tool_ctx["tool"] in _SILENT_OBS_TOOLS

        # Parse tool events and update _tool_ctx
        if non_empty:
            _output_buffer.extend(non_empty)
            while len(_output_buffer) > _BUFFER_SIZE:
                _output_buffer.pop(0)
            for event in _parse_tool_events(non_empty):
                sse_queue.put(event)

        # Snapshot obs state AFTER parsing (tells us if we just entered silent obs)
        post_silent = _tool_ctx["in_obs"] and _tool_ctx["tool"] in _SILENT_OBS_TOOLS

        if pre_silent:
            # We were already inside a silent observation → suppress the entire write
            return

        if post_silent:
            # We just entered a silent observation → emit only the lines BEFORE "Observation:"
            pre_obs: list[str] = []
            for line in lines:
                if _OBS_RE.match(line.strip()):
                    break
                pre_obs.append(line)
            if pre_obs and any(l.strip() for l in pre_obs):
                sse_queue.put({"type": "output", "text": '\n'.join(pre_obs)})
            return

        # Normal case — emit everything
        sse_queue.put({"type": "output", "text": clean})

    def flush(self):
        self._orig.flush()

    def fileno(self):
        return self._orig.fileno()

    def isatty(self):
        return self._orig.isatty()


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
