import os
from crewai.tools import BaseTool
from pydantic import BaseModel, Field


_DEFAULT_PAGE = 400   # lines per read — keeps prefill fast on local 32B models
_MAX_PAGE     = 600   # hard ceiling per call


class FileReadInput(BaseModel):
    file_path: str = Field(
        description="Path to the file. Use the full absolute path or a path relative to the project root."
    )
    start_line: int = Field(
        default=1,
        description=(
            "First line to read (1-indexed). Use this to continue reading a file that was truncated. "
            "Example: if the previous call returned '(truncado — 1344 linhas total)', "
            "call again with start_line=801 to read lines 801-1344."
        ),
    )


class ProjectFileReadTool(BaseTool):
    name: str = "read_file"
    description: str = (
        "Read the content of a file. Accepts absolute paths or paths relative to the project root. "
        "Returns up to 800 lines. If the file has more lines, the result ends with "
        "'(truncado — N linhas total)' — call again with start_line=<next line> to continue. "
        "ALWAYS read all sections of a file before drawing conclusions about what it contains."
    )
    args_schema: type[BaseModel] = FileReadInput
    project_path: str = ""

    def _run(self, file_path: str, start_line: int = 1) -> str:
        resolved = self._resolve(file_path)
        if not os.path.exists(resolved):
            alt = resolved.replace("/", os.sep).replace("\\", os.sep)
            if os.path.exists(alt):
                resolved = alt
            else:
                return (
                    f"Error: File not found at '{resolved}'. "
                    f"Make sure to use the full path including the project root ({self.project_path})."
                )
        try:
            with open(resolved, encoding="utf-8", errors="replace") as f:
                all_lines = f.read().splitlines()

            total = len(all_lines)
            start = max(1, start_line) - 1          # convert to 0-indexed
            end   = min(start + _DEFAULT_PAGE, total)

            chunk = all_lines[start:end]
            header = f"[Ficheiro: {os.path.basename(resolved)} | Linhas {start+1}–{end} de {total}]\n"
            content = header + "\n".join(chunk)

            if end < total:
                remaining = total - end
                content += (
                    f"\n\n⚠️  TRUNCADO — apenas linhas {start+1}–{end} de {total} "
                    f"({remaining} linhas restantes). "
                    f"Chama read_file novamente com start_line={end+1} para continuar."
                )
            return content
        except Exception as e:
            return f"Error reading '{resolved}': {e}"

    def _resolve(self, file_path: str) -> str:
        if os.path.isabs(file_path):
            return file_path
        file_path = file_path.replace("/", os.sep)
        if self.project_path:
            return os.path.join(self.project_path, file_path)
        return file_path
