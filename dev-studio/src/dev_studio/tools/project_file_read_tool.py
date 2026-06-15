import os
from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class FileReadInput(BaseModel):
    file_path: str = Field(
        description="Path to the file. Use the full absolute path or a path relative to the project root."
    )


class ProjectFileReadTool(BaseTool):
    name: str = "read_file"
    description: str = (
        "Read the content of a file. Accepts absolute paths or paths relative to the project root. "
        "Example: 'api/app/models/finance.py' or 'D:/Projects/FamilyHub/api/app/models/finance.py'."
    )
    args_schema: type[BaseModel] = FileReadInput
    project_path: str = ""

    def _run(self, file_path: str) -> str:
        resolved = self._resolve(file_path)
        if not os.path.exists(resolved):
            # Try the other separator in case the agent mixes / and \
            alt = resolved.replace("/", os.sep).replace("\\", os.sep)
            if os.path.exists(alt):
                resolved = alt
            else:
                return f"Error: File not found at '{resolved}'. Make sure to use the full path including the project root ({self.project_path})."
        try:
            with open(resolved, encoding="utf-8", errors="replace") as f:
                content = f.read()
            lines = content.splitlines()
            if len(lines) > 500:
                content = "\n".join(lines[:500]) + f"\n\n... (truncado — {len(lines)} linhas total)"
            return content
        except Exception as e:
            return f"Error reading '{resolved}': {e}"

    def _resolve(self, file_path: str) -> str:
        if os.path.isabs(file_path):
            return file_path
        # Normalise forward slashes on Windows
        file_path = file_path.replace("/", os.sep)
        if self.project_path:
            return os.path.join(self.project_path, file_path)
        return file_path
