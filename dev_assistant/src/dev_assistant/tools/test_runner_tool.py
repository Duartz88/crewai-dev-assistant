import os
import subprocess
from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class TestRunnerInput(BaseModel):
    project_path: str = Field(description="Root path of the project to test")


class TestRunnerTool(BaseTool):
    name: str = "run_tests"
    description: str = (
        "Run the project's existing test suite and return results. "
        "Use this after implementation to verify nothing was broken."
    )
    args_schema: type[BaseModel] = TestRunnerInput

    def _run(self, project_path: str) -> str:
        if not os.path.exists(project_path):
            return f"Caminho não encontrado: {project_path}"

        results = []

        # Python — pytest
        if self._has_python_tests(project_path):
            results.append(self._run_command(
                ["python", "-m", "pytest", "--tb=short", "-q"],
                project_path, "pytest"
            ))

        # Node/Expo — npm test (non-interactive)
        if self._has_node_tests(project_path):
            results.append(self._run_command(
                ["npm", "test", "--", "--watchAll=false", "--passWithNoTests"],
                project_path, "npm test"
            ))

        if not results:
            return "ℹ️  Nenhum teste encontrado no projeto (sem pytest.ini, pyproject.toml ou package.json com script test)."

        return "\n\n".join(results)

    def _has_python_tests(self, path: str) -> bool:
        indicators = ["pytest.ini", "pyproject.toml", "setup.cfg"]
        return any(os.path.exists(os.path.join(path, f)) for f in indicators) or \
               any(os.path.exists(os.path.join(path, "api", f)) for f in indicators)

    def _has_node_tests(self, path: str) -> bool:
        pkg = os.path.join(path, "mobile", "package.json")
        if not os.path.exists(pkg):
            pkg = os.path.join(path, "package.json")
        if not os.path.exists(pkg):
            return False
        import json
        with open(pkg) as f:
            data = json.load(f)
        return "test" in data.get("scripts", {})

    def _run_command(self, cmd: list, cwd: str, label: str) -> str:
        try:
            result = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=120
            )
            output = (result.stdout + result.stderr).strip()
            # Limit output
            lines = output.splitlines()
            if len(lines) > 50:
                output = "\n".join(lines[-50:]) + f"\n... (truncado — {len(lines)} linhas total)"
            status = "✅  PASSOU" if result.returncode == 0 else "❌  FALHOU"
            return f"### {label} — {status}\n```\n{output}\n```"
        except subprocess.TimeoutExpired:
            return f"### {label} — ⏱️  TIMEOUT (>120s)"
        except FileNotFoundError:
            return f"### {label} — ⚠️  Comando não encontrado"
