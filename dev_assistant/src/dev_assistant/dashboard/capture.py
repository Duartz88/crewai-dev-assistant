"""
Intercepts sys.stdout and builtins.input so the crew's output and approval
prompts can be streamed to the browser via SSE.
"""
import builtins
import queue
import re
import sys
import threading

# ── Global state ──────────────────────────────────────────────────────────────

sse_queue: queue.Queue = queue.Queue()       # text lines → browser
input_prompt_event = threading.Event()       # signals browser "input needed"
input_response_queue: queue.Queue = queue.Queue(maxsize=1)

_current_prompt: str = ""
_dashboard_active = False
_original_input = builtins.input
_output_buffer: list[str] = []              # últimas N linhas de output
_BUFFER_SIZE = 80

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


# ── stdout capture ────────────────────────────────────────────────────────────

class _TeeOutput:
    """Write to the original stdout AND push stripped lines to the SSE queue."""

    def __init__(self, original):
        self._orig = original

    def write(self, text: str):
        self._orig.write(text)
        clean = strip_ansi(text)
        if clean.strip():
            sse_queue.put({"type": "output", "text": clean})
            # Keep rolling buffer for context in input prompts
            for line in clean.splitlines():
                if line.strip():
                    _output_buffer.append(line)
            while len(_output_buffer) > _BUFFER_SIZE:
                _output_buffer.pop(0)

    def flush(self):
        self._orig.flush()

    # Rich checks fileno() to decide if it's a TTY
    def fileno(self):
        return self._orig.fileno()

    def isatty(self):
        return self._orig.isatty()


# ── input() intercept ─────────────────────────────────────────────────────────

# O CrewAI chama input() SEM argumentos para o human_input loop (linha 364 de
# human_input.py). Todos os nossos próprios prompts (aprovação de plano, diff
# de ficheiro) têm sempre texto no argumento. Logo: prompt vazio = CrewAI interno.
# Retornar "" faz o CrewAI sair do loop sem chamar _format_feedback_message
# (que não existe no experimental/agent_executor.py da versão 1.14.7).
def _is_crewai_human_input(prompt: str) -> bool:
    return not prompt.strip()


def _dashboard_input(prompt: str = "") -> str:
    global _current_prompt
    clean_prompt = strip_ansi(str(prompt))

    # Auto-aceitar human_input interno do CrewAI.
    # "" faz o CrewAI sair do loop sem chamar _format_feedback_message.
    if _is_crewai_human_input(clean_prompt):
        sse_queue.put({"type": "output", "text": "💬 [auto] CrewAI human_input aceite automaticamente (Enter)"})
        return ""

    _current_prompt = clean_prompt
    # Include last output lines as context so the browser can show them in the modal
    context = "\n".join(_output_buffer[-40:])
    sse_queue.put({"type": "input_needed", "prompt": clean_prompt, "context": context})
    input_prompt_event.set()
    # Block until browser responds (up to 10 min)
    try:
        response = input_response_queue.get(timeout=600)
    except queue.Empty:
        response = ""
    # Echo the response to the output stream so it appears in the dashboard log
    sse_queue.put({"type": "output", "text": f"{clean_prompt}{response}"})
    return response


# ── Enable / disable ─────────────────────────────────────────────────────────

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
