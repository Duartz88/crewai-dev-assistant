import subprocess
from datetime import datetime


def run_git(args: list, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def create_session_branch(project_path: str) -> tuple[str | None, str | None]:
    """Create a timestamped crew/session-* branch. Returns (branch, error)."""
    if run_git(["status"], project_path).returncode != 0:
        return None, "Not a git repository"
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    branch = f"crew/session-{stamp}"
    result = run_git(["checkout", "-b", branch], project_path)
    if result.returncode == 0:
        return branch, None
    return None, result.stderr.strip()
