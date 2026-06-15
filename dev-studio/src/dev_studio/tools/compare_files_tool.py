import difflib
import os
from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class CompareInput(BaseModel):
    file_a: str = Field(description="Path to the reference file (e.g. the working version)")
    file_b: str = Field(description="Path to the file to compare against the reference")
    context_lines: int = Field(default=5, description="Lines of context around each difference")


class CompareFilesTool(BaseTool):
    name: str = "compare_files"
    description: str = (
        "Compare two files and return a unified diff showing all differences line by line. "
        "Essential for cross-project comparisons, regression detection, and finding bugs "
        "introduced relative to a known-good reference. "
        "Lines starting with '-' exist only in file_a; '+' only in file_b."
    )
    args_schema: type[BaseModel] = CompareInput

    def _run(self, file_a: str, file_b: str, context_lines: int = 5) -> str:
        def _read(path: str) -> tuple[list[str] | None, str | None]:
            if not os.path.exists(path):
                return None, f"Ficheiro não encontrado: '{path}'"
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    return f.readlines(), None
            except Exception as e:
                return None, str(e)

        lines_a, err_a = _read(file_a)
        if err_a:
            return f"ERRO ao ler ficheiro A ({file_a}): {err_a}"

        lines_b, err_b = _read(file_b)
        if err_b:
            return f"ERRO ao ler ficheiro B ({file_b}): {err_b}"

        diff = list(difflib.unified_diff(
            lines_a, lines_b,
            fromfile=f"A: {file_a}",
            tofile=f"B: {file_b}",
            n=context_lines,
        ))

        if not diff:
            return f"Ficheiros idênticos: '{os.path.basename(file_a)}' == '{os.path.basename(file_b)}'"

        MAX_LINES = 600
        header = (
            f"DIFF: {os.path.basename(file_a)} vs {os.path.basename(file_b)}\n"
            f"Total de diferenças: {sum(1 for l in diff if l.startswith(('+', '-')) and not l.startswith(('---', '+++')))} linhas\n"
            f"{'=' * 60}\n"
        )

        body = "".join(diff[:MAX_LINES])
        suffix = (
            f"\n... (truncado — {len(diff)} linhas de diff total. "
            "Usa context_lines=2 para ver mais diferenças de uma vez.)"
            if len(diff) > MAX_LINES else ""
        )

        return header + body + suffix
