import os
from typing import Any
from crewai import Agent, Crew, Process, Task, LLM
from crewai.project import CrewBase, agent, crew, task
from dev_studio.tools.project_file_read_tool import ProjectFileReadTool
from dev_studio.tools.smart_directory_tool import SmartDirectoryTool
from dev_studio.tools.diff_writer_tool import DiffFileWriterTool
from dev_studio.tools.syntax_validator_tool import PythonSyntaxValidatorTool
from dev_studio.tools.typescript_validator_tool import TypeScriptValidatorTool
from dev_studio.tools.powershell_validator_tool import ValidatePowerShellTool
from dev_studio.tools.grep_tool import GrepInProjectTool
from dev_studio.tools.compare_files_tool import CompareFilesTool
from dev_studio.tools.test_runner_tool import TestRunnerTool
from dev_studio.tools.git_log_tool import GitLogTool
from dev_studio.tools.project_memory_tool import ProjectMemoryReadTool, ProjectMemoryWriteTool
from dev_studio.tools.patch_file_tool import PatchFileTool
from dev_studio.tools.endpoint_verify_tool import EndpointVerifyTool
from dev_studio.tools.project_scanner_tool import ProjectScannerTool
from dev_studio.tools.project_state_tool import ProjectStateReadTool
from dev_studio.models import ArchitecturePlan, ImplementationResult, ReviewResult

from dev_studio.utils.settings import load_settings as _load_settings  # noqa: E402


def _build_llms() -> tuple:
    """Build LLM instances from current settings (re-read on every crew instantiation)."""
    s = _load_settings()
    url   = s["lm_base_url"]
    key   = s["lm_api_key"]
    model = s["model_name"] or "default"
    return (
        LLM(model=model, base_url=url, api_key=key, temperature=0.4,  extra_body={"enable_thinking": True}),   # type: ignore[call-overload]
        LLM(model=model, base_url=url, api_key=key, temperature=0.15, extra_body={"enable_thinking": False}),  # type: ignore[call-overload]
        LLM(model=model, base_url=url, api_key=key, temperature=0.35, extra_body={"enable_thinking": True}),   # type: ignore[call-overload]
    )

dir_tool     = SmartDirectoryTool()
py_validator = PythonSyntaxValidatorTool()
ts_validator = TypeScriptValidatorTool()
ps_validator = ValidatePowerShellTool()
grep_tool    = GrepInProjectTool()
compare_tool = CompareFilesTool()
test_runner  = TestRunnerTool()
git_log      = GitLogTool()
mem_read     = ProjectMemoryReadTool()
mem_write    = ProjectMemoryWriteTool()


def _load_rules(project_path: str) -> str:
    """Load rules as a compact bullet list to minimise context usage."""
    lines = []
    global_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "GLOBAL_RULES.md")
    if os.path.exists(global_path):
        with open(global_path, encoding="utf-8") as f:
            lines.append("REGRAS GLOBAIS:\n" + f.read().strip())
    project_rules = os.path.join(project_path, "CREW_RULES.md")
    if os.path.exists(project_rules):
        with open(project_rules, encoding="utf-8") as f:
            lines.append("REGRAS DO PROJETO:\n" + f.read().strip())
    return ("\n\n" + "\n\n".join(lines)) if lines else ""


@CrewBase
class DevStudioCrew:
    agents_config: Any = "config/agents.yaml"
    tasks_config:  Any = "config/tasks.yaml"

    def __init__(self, project_path: str = ""):
        self._rules        = _load_rules(project_path)
        self._llm_a, self._llm_d, self._llm_r = _build_llms()
        self._file_read    = ProjectFileReadTool(project_path=project_path)
        self._file_write   = DiffFileWriterTool(project_path=project_path)
        self._file_patch   = PatchFileTool(project_path=project_path)
        self._ep_verify    = EndpointVerifyTool(project_path=project_path)
        self._scanner      = ProjectScannerTool(project_path=project_path)
        self._state_read   = ProjectStateReadTool(project_path=project_path)

    def _cfg(self, key: str) -> dict:
        cfg: dict[str, Any] = dict(self.agents_config[key])  # type: ignore[call-overload]
        if self._rules:
            cfg["backstory"] = cfg["backstory"] + "\n\n" + self._rules
        return cfg

    @agent
    def architect(self) -> Agent:
        return Agent(
            config=self._cfg("architect"),
            llm=self._llm_a,
            tools=[
                self._state_read,                 # structured index: routes/services/models/components
                self._scanner,                    # re-scan project map on demand
                dir_tool, self._file_read, git_log, mem_read,
                grep_tool, compare_tool,          # cross-project analysis
                self._ep_verify,                  # verify API endpoints before plan
                ps_validator,                     # syntax check (PS1/PSM1)
            ],
            verbose=True,
            max_iter=15,
        )

    @agent
    def developer(self) -> Agent:
        return Agent(
            config=self._cfg("developer"),
            llm=self._llm_d,
            tools=[
                dir_tool, self._file_read,
                self._file_patch,   # PREFERÊNCIA: modifica ficheiros existentes com patch_file
                self._file_write,   # apenas para criar ficheiros novos
                py_validator, ts_validator, ps_validator,
                self._ep_verify,    # verificar endpoints antes de os usar
                mem_write,
            ],
            verbose=True,
            max_iter=25,
        )

    @agent
    def reviewer(self) -> Agent:
        return Agent(
            config=self._cfg("reviewer"),
            llm=self._llm_r,
            tools=[
                self._state_read,                           # verify routes/services exist
                dir_tool, self._file_read, test_runner, mem_write,
                grep_tool, compare_tool, ps_validator,      # deep review
            ],
            verbose=True,
            max_iter=8,
        )

    @task
    def design_task(self) -> Task:
        return Task(
            config=self.tasks_config["design_task"],      # type: ignore[call-arg]
            output_pydantic=ArchitecturePlan,
        )

    @task
    def implement_task(self) -> Task:
        return Task(
            config=self.tasks_config["implement_task"],   # type: ignore[call-arg]
            output_pydantic=ImplementationResult,
        )

    @task
    def fix_task(self) -> Task:
        return Task(
            config=self.tasks_config["fix_task"],         # type: ignore[call-arg]
            output_pydantic=ImplementationResult,
        )

    @task
    def review_task(self) -> Task:
        return Task(
            config=self.tasks_config["review_task"],      # type: ignore[call-arg]
            output_pydantic=ReviewResult,
        )

    @crew
    def crew(self) -> Crew:
        return Crew(
            agents=[self.architect()],
            tasks=[self.design_task()],
            process=Process.sequential,
            verbose=True,
        )

    def implement_crew(self) -> Crew:
        return Crew(
            agents=[self.developer()],
            tasks=[self.implement_task()],
            process=Process.sequential,
            verbose=True,
        )

    def review_crew(self) -> Crew:
        return Crew(
            agents=[self.reviewer()],
            tasks=[self.review_task()],
            process=Process.sequential,
            verbose=True,
        )

    def fix_only_crew(self) -> Crew:
        """Developer-only fix crew — applies reviewer issues via fix_task.
        Reviewer runs separately so we can capture both outputs."""
        return Crew(
            agents=[self.developer()],
            tasks=[self.fix_task()],
            process=Process.sequential,
            verbose=True,
        )

    def fix_crew(self) -> Crew:
        return Crew(
            agents=[self.developer(), self.reviewer()],
            tasks=[self.fix_task(), self.review_task()],
            process=Process.sequential,
            verbose=True,
        )

    def dev_review_crew(self) -> Crew:
        """Developer + Reviewer in sequence. Context = architect plan."""
        return Crew(
            agents=[self.developer(), self.reviewer()],
            tasks=[self.implement_task(), self.review_task()],
            process=Process.sequential,
            verbose=True,
        )

    def arch_review_crew(self) -> Crew:
        """Architect + Reviewer in sequence with reduced max_iter to avoid context overflow."""
        arch = Agent(
            config=self._cfg("architect"),
            llm=self._llm_a,
            tools=[
                self._scanner,
                dir_tool, self._file_read, git_log, mem_read,
                grep_tool, compare_tool,
                self._ep_verify,
                ps_validator,
            ],
            verbose=True,
            max_iter=10,
        )
        rev = Agent(
            config=self._cfg("reviewer"),
            llm=self._llm_r,
            tools=[
                dir_tool, self._file_read,
                grep_tool, compare_tool, ps_validator, mem_write,
            ],
            verbose=True,
            max_iter=4,
        )
        return Crew(
            agents=[arch, rev],
            tasks=[self.design_task(), self.review_task()],
            process=Process.sequential,
            verbose=True,
        )
