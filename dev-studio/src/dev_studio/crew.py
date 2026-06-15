import os
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

_BASE_URL = "https://duartz-pc.tail63c43f.ts.net/v1"
_API_KEY  = "sk-lm-TTrbPXEj:VO2I1WD72FJSCDQSAWQ0"

# Switch between models here — Gemma for dev/review, Claude for architecture
_MODEL_ARCHITECT = "gemma-4-26b-a4b"
_MODEL_DEVELOPER = "gemma-4-26b-a4b"
_MODEL_REVIEWER  = "gemma-4-26b-a4b"

# Gemma works better with slightly higher temperatures than Claude
# (too low → repetitive/stuck loops; too high → hallucinations)
llm_architect = LLM(model=_MODEL_ARCHITECT, base_url=_BASE_URL, api_key=_API_KEY, temperature=0.4)
llm_developer = LLM(model=_MODEL_DEVELOPER, base_url=_BASE_URL, api_key=_API_KEY, temperature=0.2)
llm_reviewer  = LLM(model=_MODEL_REVIEWER,  base_url=_BASE_URL, api_key=_API_KEY, temperature=0.3)

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
    agents_config = "config/agents.yaml"
    tasks_config  = "config/tasks.yaml"

    def __init__(self, project_path: str = ""):
        self._rules = _load_rules(project_path)
        self._file_read  = ProjectFileReadTool(project_path=project_path)
        self._file_write = DiffFileWriterTool(project_path=project_path)

    def _cfg(self, key: str) -> dict:
        cfg = dict(self.agents_config[key])
        if self._rules:
            cfg["backstory"] = cfg["backstory"] + "\n\n" + self._rules
        return cfg

    @agent
    def architect(self) -> Agent:
        return Agent(
            config=self._cfg("architect"),
            llm=llm_architect,
            tools=[
                dir_tool, self._file_read, git_log, mem_read,
                grep_tool, compare_tool,          # cross-project analysis
            ],
            verbose=True,
            max_iter=10,
        )

    @agent
    def developer(self) -> Agent:
        return Agent(
            config=self._cfg("developer"),
            llm=llm_developer,
            tools=[
                dir_tool, self._file_read, self._file_write,
                py_validator, ts_validator, ps_validator,   # validate before write
                mem_write,
            ],
            verbose=True,
            max_iter=12,
        )

    @agent
    def reviewer(self) -> Agent:
        return Agent(
            config=self._cfg("reviewer"),
            llm=llm_reviewer,
            tools=[
                dir_tool, self._file_read, test_runner, mem_write,
                grep_tool, compare_tool, ps_validator,      # deep review
            ],
            verbose=True,
            max_iter=8,
        )

    @task
    def design_task(self) -> Task:
        return Task(config=self.tasks_config["design_task"])

    @task
    def implement_task(self) -> Task:
        return Task(config=self.tasks_config["implement_task"])

    @task
    def fix_task(self) -> Task:
        return Task(config=self.tasks_config["fix_task"])

    @task
    def review_task(self) -> Task:
        return Task(config=self.tasks_config["review_task"])

    @crew
    def crew(self) -> Crew:
        return Crew(
            agents=[self.architect()],
            tasks=[self.design_task()],
            process=Process.sequential,
            verbose=True,
            max_rpm=10,
        )

    def implement_crew(self) -> Crew:
        return Crew(
            agents=[self.developer()],
            tasks=[self.implement_task()],
            process=Process.sequential,
            verbose=True,
            max_rpm=10,
        )

    def review_crew(self) -> Crew:
        return Crew(
            agents=[self.reviewer()],
            tasks=[self.review_task()],
            process=Process.sequential,
            verbose=True,
            max_rpm=10,
        )

    def fix_crew(self) -> Crew:
        return Crew(
            agents=[self.developer(), self.reviewer()],
            tasks=[self.fix_task(), self.review_task()],
            process=Process.sequential,
            verbose=True,
            max_rpm=10,
        )

    def dev_review_crew(self) -> Crew:
        """Developer + Reviewer in sequence. Context = architect plan."""
        return Crew(
            agents=[self.developer(), self.reviewer()],
            tasks=[self.implement_task(), self.review_task()],
            process=Process.sequential,
            verbose=True,
            max_rpm=10,
        )
