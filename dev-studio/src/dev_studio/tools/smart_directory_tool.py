import os
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

IGNORE = {
    "node_modules", ".git", "dist", "build", "__pycache__",
    ".venv", "venv", ".angular", "bin", "obj", ".vs",
    "packages", ".next", "coverage", ".nyc_output",
}


class SmartDirectoryInput(BaseModel):
    path: str = Field(description="Directory path to list")
    max_depth: int = Field(default=3, description="Max depth to traverse")


class SmartDirectoryTool(BaseTool):
    name: str = "list_project_structure"
    description: str = (
        "List the project directory structure, ignoring dependency folders "
        "like node_modules, .git, dist, build, __pycache__, .venv, bin, obj."
    )
    args_schema: type[BaseModel] = SmartDirectoryInput

    def _run(self, path: str, max_depth: int = 3) -> str:
        if not os.path.exists(path):
            return f"Path not found: {path}"
        lines = []
        self._walk(path, lines, 0, max_depth)
        return "\n".join(lines) if lines else "Empty directory"

    def _walk(self, path: str, lines: list, depth: int, max_depth: int):
        if depth > max_depth:
            return
        try:
            entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name))
        except PermissionError:
            return
        for entry in entries:
            if entry.name in IGNORE or entry.name.startswith("."):
                continue
            indent = "  " * depth
            if entry.is_dir():
                lines.append(f"{indent}{entry.name}/")
                self._walk(entry.path, lines, depth + 1, max_depth)
            else:
                lines.append(f"{indent}{entry.name}")
