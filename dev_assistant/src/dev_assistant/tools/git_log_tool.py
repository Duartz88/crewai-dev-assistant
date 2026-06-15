import subprocess
from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class GitLogInput(BaseModel):
    project_path: str = Field(description="Root path of the git repository")
    limit: int = Field(default=15, description="Number of recent commits to show")


class GitLogTool(BaseTool):
    name: str = "git_log"
    description: str = (
        "Show recent git commits and current uncommitted changes for a project. "
        "Use this to understand what changed recently before proposing modifications."
    )
    args_schema: type[BaseModel] = GitLogInput

    def _run(self, project_path: str, limit: int = 15) -> str:
        parts = []

        log = self._git(
            ["log", f"-{limit}", "--oneline", "--no-merges", "--decorate"],
            project_path
        )
        if log:
            parts.append(f"## Commits recentes\n```\n{log}\n```")

        diff_stat = self._git(["diff", "HEAD", "--stat"], project_path)
        if diff_stat:
            parts.append(f"## Alterações não commitadas\n```\n{diff_stat}\n```")

        untracked = self._git(
            ["ls-files", "--others", "--exclude-standard"], project_path
        )
        if untracked:
            parts.append(f"## Ficheiros novos (não rastreados)\n```\n{untracked}\n```")

        return "\n\n".join(parts) if parts else "ℹ️  Sem histórico git disponível."

    def _git(self, args: list, cwd: str) -> str:
        try:
            result = subprocess.run(
                ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=10
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""
