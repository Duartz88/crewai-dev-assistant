import fnmatch
import os
import re
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

_IGNORE = {
    "node_modules", ".git", "dist", "build", "__pycache__",
    ".venv", "venv", ".angular", "bin", "obj", ".vs",
    "packages", ".next", "coverage", ".nyc_output",
}


class GrepInput(BaseModel):
    pattern: str = Field(description="Regex or literal string to search for")
    path: str = Field(description="Directory or file path to search in")
    file_glob: str = Field(default="*", description="File filter, e.g. '*.ps1', '*.py', '*.ts'")
    case_sensitive: bool = Field(default=False, description="Case-sensitive match (default: false)")
    max_results: int = Field(default=150, description="Maximum number of matching lines to return")


class GrepInProjectTool(BaseTool):
    name: str = "grep_in_files"
    description: str = (
        "Search for a pattern (regex or literal string) across files in a directory. "
        "Returns matching lines with file path and line number. "
        "Use to find function definitions, variable usages, bug patterns, "
        "parameter names, or any text across the project. "
        "Also works on external project paths for cross-project searches."
    )
    args_schema: type[BaseModel] = GrepInput

    def _run(self, pattern: str, path: str, file_glob: str = "*",
             case_sensitive: bool = False, max_results: int = 150) -> str:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return f"Padrão regex inválido '{pattern}': {e}"

        if not os.path.exists(path):
            return f"Caminho não encontrado: '{path}'"

        files: list[str] = []
        if os.path.isfile(path):
            files = [path]
        else:
            for root, dirs, filenames in os.walk(path):
                dirs[:] = [d for d in dirs if d not in _IGNORE and not d.startswith(".")]
                for name in filenames:
                    if fnmatch.fnmatch(name, file_glob):
                        files.append(os.path.join(root, name))

        results: list[str] = []
        base = path if os.path.isdir(path) else os.path.dirname(path)

        for filepath in sorted(files):
            try:
                with open(filepath, encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if regex.search(line):
                            rel = os.path.relpath(filepath, base)
                            results.append(f"{rel}:{lineno}: {line.rstrip()}")
                            if len(results) >= max_results:
                                results.append(
                                    f"\n... truncado — mais de {max_results} resultados. "
                                    "Usa um padrão mais específico ou file_glob para filtrar."
                                )
                                return "\n".join(results)
            except Exception:
                continue

        if not results:
            return f"Nenhum resultado para '{pattern}' em '{path}'"
        return "\n".join(results)
