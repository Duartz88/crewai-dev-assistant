"""
Intercepts sys.stdout and builtins.input so crew output and approval
prompts can be streamed to the browser via SSE.
"""
import atexit
import builtins
import os
import queue
import re
import sys
import threading
import time

sse_queue: queue.Queue = queue.Queue()
input_prompt_event = threading.Event()
input_response_queue: queue.Queue = queue.Queue(maxsize=1)
_cancel_requested = threading.Event()

# ── Raw agent log (pre-filter, pre-SSE) ───────────────────────────────────────
_LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
_LOG_PATH = os.path.normpath(os.path.join(_LOG_DIR, "dev_studio_agents.log"))
_agent_log = open(_LOG_PATH, "w", encoding="utf-8", buffering=1)  # noqa: WPS515
atexit.register(_agent_log.close)

# ── <think> tag defensive suppression ─────────────────────────────────────────
_THINK_BLOCK_RE = re.compile(r'<think>.*?</think>', re.DOTALL | re.IGNORECASE)

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
    r'|<\|im_(?:start|end|sep)\|>'  # model chat template tokens leaking into output
    r'|Args:\s*\{.*'            # plain-text "Args: {...}" emitted by CrewAI event bus (dupe of box content)
    r'|Tool:\s+\S'              # plain-text "Tool: <name>" dupe from event bus
    r'|Your output must be in the exact format'  # CrewAI pydantic format reminder echoed by model
    r')',
    re.IGNORECASE,
)

# Tools whose file/search content is suppressed from the SSE output stream.
# A compact tool card with path is shown instead.
_SILENT_OBS_TOOLS = frozenset({
    "read_file",
    "list_project_structure",
    "read_project_memory",
    "compare_files",
    "read_project_state",
    "grep_in_files",        # can return hundreds of matching lines
    "grep_in_project",      # alias used in some configs
})


def _should_skip_box(title: str) -> bool:
    t = title.strip().lower()
    return any(t == p or t.startswith(p + " ") or t.startswith(p + "(") for p in _BOX_SKIP_PREFIXES)


# ── Native tool tracking (function-calling / non-ReAct style) ─────────────────
_native_tool_args: dict[str, str] = {}      # tool_name → last raw args string
_native_pending: list[dict] = []            # events queued inside _clean_rich_box

_FILE_PATH_RE = re.compile(r"(?:file_path|file_a|directory_path)['\"\s:]+([^'\"\}\n,]+)")

# Detect the distinctive first line of compare_files output so it can be
# suppressed even when the suppress_content flag was cleared prematurely
# (e.g. LLM emitted "Final answer will..." in its thinking).
_COMPARE_FIRST_LINE_RE = re.compile(
    r'^(DIFF:\s+\S.*\svs\s+\S|Ficheiros id[eê]nticos:|ERRO ao ler ficheiro)',
    re.IGNORECASE,
)


def _extract_path(args: str) -> str:
    """Pull a file path out of a raw Args string like {'file_path': 'D:/...'}."""
    m = _FILE_PATH_RE.search(args)
    return m.group(1).strip("'\"/ \\").replace("\\\\", "\\") if m else args[:200]


# ── Rich box → clean text ──────────────────────────────────────────────────────
# Box parsing state is MODULE-LEVEL so it persists across write() calls.
# If a Rich panel is split across two write() calls (first call ends mid-box),
# the state is preserved correctly for the second call.
_box: dict = {
    "in_skip":            False,   # currently inside a skip-listed box
    "in_tool_start":      False,   # currently inside a "Tool Execution Started" box
    "last_title":         "",      # lowercase title of current box
    "ts_info":            {"name": "", "args": ""},  # scratch for tool-start parsing
    "suppress_content":   False,   # suppress plain-text tool output until "completed"
    "im_block":           False,   # suppress everything between <|im_start|> and <|im_end|>
}

# Tracks timing of the currently-executing tool so a tool_done SSE event can
# be emitted with the elapsed seconds when "Tool Execution Completed" closes.
_tool_timing: dict = {"name": "", "n": 0, "start": 0.0}

# Per-request tool usage stats, reset by app.py at the start of each request.
# {tool_name: {"count": n, "total_secs": s}}
_request_tool_stats: dict = {}


def _record_tool_stat(name: str, secs: float) -> None:
    entry = _request_tool_stats.setdefault(name, {"count": 0, "total_secs": 0.0})
    entry["count"] += 1
    entry["total_secs"] = round(entry["total_secs"] + secs, 1)


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

            if _box["suppress_content"]:
                if _box["in_tool_start"]:
                    # New tool call: end previous silent tool's content window and
                    # start tracking the new tool (box content must still be captured).
                    _box["suppress_content"] = False
                    _box["in_skip"] = True
                    _box["ts_info"] = {"name": "", "args": ""}
                else:
                    # ALL other boxes (Tool Execution Completed, agent step, task, crew, etc.)
                    # are skipped and DO NOT clear suppress_content.
                    # suppress_content only clears on:
                    #   1. A new "Tool Execution Started" ╰ (new tool call)
                    #   2. "Final Answer:" appearing as plain text
                    # This prevents intermediate boxes from prematurely exposing tool content.
                    _box["in_skip"] = True
                    _box["in_tool_start"] = False
            elif _box["in_tool_start"]:
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
                # If the previous tool never got a "Tool Execution Completed" box
                # (can happen when a tool fails mid-run or the box is suppressed),
                # emit its tool_done now before overwriting the timing state.
                if _tool_timing["name"] and _tool_timing["name"] != name:
                    elapsed = round(time.time() - _tool_timing["start"], 1)
                    sse_queue.put({
                        "type": "tool_done",
                        "tool": _tool_timing["name"],
                        "secs": elapsed,
                        "n":    _tool_timing["n"],
                    })
                    _record_tool_stat(_tool_timing["name"], elapsed)
                # Record start time so tool_done can carry elapsed seconds.
                _tool_timing["n"] += 1
                _tool_timing["name"] = name
                _tool_timing["start"] = time.time()
                _native_pending.append({"type": "tool_call", "tool": name,
                                        "n": _tool_timing["n"]})
                if args:
                    _native_pending.append({
                        "type": "tool_input", "tool": name,
                        "input": _extract_path(args) if name in _SILENT_OBS_TOOLS else args[:400],
                    })
                # Activate content suppression for silent tools
                if name in _SILENT_OBS_TOOLS:
                    _box["suppress_content"] = True
            elif _box["last_title"] == "tool execution completed" and _tool_timing["name"]:
                # "Tool Execution Completed" box just closed — emit timing.
                elapsed = round(time.time() - _tool_timing["start"], 1)
                sse_queue.put({
                    "type": "tool_done",
                    "tool": _tool_timing["name"],
                    "secs": elapsed,
                    "n":    _tool_timing["n"],
                })
                _record_tool_stat(_tool_timing["name"], elapsed)
                _tool_timing["name"] = ""
            _box["in_skip"] = False
            _box["in_tool_start"] = False
            continue

        # ── Content line: │  text  │ ──────────────────────────────────────
        if s.startswith('│'):
            content = s.lstrip('│').rstrip('│').strip()
            if not content:
                continue
            if _box["in_tool_start"]:
                # "Tool: " is the canonical prefix; "Name: " appears in some CrewAI versions.
                if content.startswith('Tool: ') or (content.startswith('Name: ') and not _box["ts_info"]["name"]):
                    _box["ts_info"]["name"] = content[content.index(': ') + 2:].strip()
                elif content.startswith('Args: ') or content.startswith('Arguments: '):
                    _box["ts_info"]["args"] = content[content.index(': ') + 2:].strip()
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
            if not prev_blank and not _box["suppress_content"] and not _box["im_block"]:
                out.append('')
                prev_blank = True
        else:
            sl = s.lower()

            # Detect <|im_start|> token: everything from here until <|im_end|> is
            # a model template artefact (usually a duplicate of the final answer).
            if '<|im_start|>' in s:
                _box["im_block"] = True
                continue
            if _box["im_block"]:
                if '<|im_end|>' in s:
                    _box["im_block"] = False
                continue

            if sl in _PLAIN_SKIP:
                continue
            # Bare silent-tool name on its own line ("compare_files", "read_file" etc.)
            # CrewAI event bus prints the tool name as a standalone line alongside the Rich box.
            if s in _SILENT_OBS_TOOLS:
                continue
            # Check prefix-based noise patterns (e.g. "[Finalize] ...")
            # Also suppress numbered variants: "Tool Execution Started (#1)"
            if _PLAIN_SKIP_RE.match(s):
                continue
            if any(sl.startswith(p) for p in ("tool execution started", "tool execution completed")):
                continue
            # Suppress plain-text tool result content for silent tools.
            # Active from the ╰ of a silent "Tool Execution Started" until:
            #   - the next tool call begins (handled in ╭ handler above), OR
            #   - "Final Answer:" appears (model's final response), OR
            #   - a non-skip-listed box appears (handled in ╭ handler above).
            # Note: "Tool Completed" / "Tool Execution Completed" do NOT end suppression
            # because the actual tool content (e.g. a diff) arrives AFTER those signals.
            if _box["suppress_content"]:
                # Only clear on the actual "Final Answer:" marker (with colon).
                # Avoids false positives like "Final answer will include..." in LLM thinking.
                if sl.startswith("final answer:"):
                    _box["suppress_content"] = False
                    # fall through — let "Final Answer:" line be processed normally
                else:
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

    # If the "Tool Execution Started" box was already parsed, the card was emitted
    # via _native_pending — skip re-emission to avoid duplicates on the frontend.
    if tool_name not in _native_tool_args:
        sse_queue.put({"type": "tool_call",  "tool": tool_name})
        sse_queue.put({"type": "tool_input", "tool": tool_name, "input": tool_name})

    # Fallback timing: the "Tool Execution Completed" box didn't emit tool_done
    # (e.g. no Rich boxes in this run), so emit it here from the result line.
    if _tool_timing["name"]:
        elapsed = round(time.time() - _tool_timing["start"], 1)
        sse_queue.put({
            "type": "tool_done",
            "tool": _tool_timing["name"],
            "secs": elapsed,
            "n":    _tool_timing["n"],
        })
        _record_tool_stat(_tool_timing["name"], elapsed)
        _tool_timing["name"] = ""

    # Activate suppress_content so that any subsequent write() calls carrying
    # the continuation of this result (multi-chunk native results) are also
    # suppressed by Safety Net A — not just the line that matched this pattern.
    # The flag is cleared by the next "Tool Execution Started" box or "Final Answer:".
    _box["suppress_content"] = True

    return True  # always suppress the raw content line


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


# ── stdout capture ─────────────────────────────────────────────────────────────

class _TeeOutput:
    def __init__(self, original):
        self._orig = original

    def write(self, text: str):
        if _cancel_requested.is_set():
            # Do NOT clear here — the background thread's finally block clears it
            # after the KeyboardInterrupt is caught, so a second cancel request
            # between is_set() and clear() can't be silently dropped.
            raise KeyboardInterrupt("Pedido cancelado pelo utilizador")

        self._orig.write(text)

        ansi_clean = strip_ansi(text)
        # Write raw (ANSI-stripped) output to log file before any filtering.
        _agent_log.write(ansi_clean)
        # Suppress <think>...</think> blocks before SSE (defensive — LM Studio's
        # enable_thinking:false should handle this, but guard against fallthrough).
        ansi_clean = _THINK_BLOCK_RE.sub('', ansi_clean)
        clean = _clean_rich_box(ansi_clean)

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

        # Safety net A: suppress_content is still active but _clean_rich_box let
        # content through (happens when the diff arrives in the same write() call
        # as the box ╭ but before the ╰ sets suppress_content, or when the LLM
        # emitted "final answer" without a colon in its thinking and cleared the flag).
        if _box["suppress_content"]:
            has_final = any(l.strip().lower().startswith("final answer:") for l in non_empty)
            if not has_final:
                return

        # Safety net B: detect compare_files output by its distinctive first line,
        # even when suppress_content was already cleared prematurely.
        first_nonempty = non_empty[0].strip() if non_empty else ""
        if _COMPARE_FIRST_LINE_RE.match(first_nonempty):
            return  # compare_files diff/result — always suppress raw output

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
