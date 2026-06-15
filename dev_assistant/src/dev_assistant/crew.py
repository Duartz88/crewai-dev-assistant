import os
from crewai import Agent, Crew, Process, Task, LLM
from crewai.project import CrewBase, agent, crew, task
from dev_assistant.tools.project_file_read_tool import ProjectFileReadTool
from dev_assistant.tools.smart_directory_tool import SmartDirectoryTool
from dev_assistant.tools.diff_writer_tool import DiffFileWriterTool
from dev_assistant.tools.syntax_validator_tool import PythonSyntaxValidatorTool
from dev_assistant.tools.typescript_validator_tool import TypeScriptValidatorTool
from dev_assistant.tools.test_runner_tool import TestRunnerTool
from dev_assistant.tools.git_log_tool import GitLogTool
from dev_assistant.tools.project_memory_tool import ProjectMemoryReadTool, ProjectMemoryWriteTool

_BASE_URL = "http://localhost:1234/v1"
_API_KEY = "sk-lm-TTrbPXEj:VO2I1WD72FJSCDQSAWQ0"
_MODEL = "claude-opus-4.8"

# Temperaturas diferenciadas por papel (mesmo modelo, comportamentos distintos)
llm_architect = LLM(model=_MODEL, base_url=_BASE_URL, api_key=_API_KEY, temperature=0.3)
llm_developer = LLM(model=_MODEL, base_url=_BASE_URL, api_key=_API_KEY, temperature=0.1)
llm_reviewer  = LLM(model=_MODEL, base_url=_BASE_URL, api_key=_API_KEY, temperature=0.2)

file_write      = DiffFileWriterTool()
dir_tool        = SmartDirectoryTool()
py_validator    = PythonSyntaxValidatorTool()
ts_validator    = TypeScriptValidatorTool()
test_runner     = TestRunnerTool()
git_log         = GitLogTool()
mem_read        = ProjectMemoryReadTool()
mem_write       = ProjectMemoryWriteTool()


def _load_rules(project_path: str) -> str:
    result = ""
    global_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "GLOBAL_RULES.md")
    if os.path.exists(global_path):
        with open(global_path, encoding="utf-8") as f:
            result += f"\n\n## REGRAS GLOBAIS (obrigatórias)\n{f.read()}"
    project_rules = os.path.join(project_path, "CREW_RULES.md")
    if os.path.exists(project_rules):
        with open(project_rules, encoding="utf-8") as f:
            result += f"\n\n## REGRAS DESTE PROJETO (obrigatórias)\n{f.read()}"
    return result


@CrewBase
class DevAssistantCrew:
    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def __init__(self, project_path: str = ""):
        self._rules = _load_rules(project_path)
        self._file_read = ProjectFileReadTool(project_path=project_path)

    def _cfg(self, key: str) -> dict:
        cfg = dict(self.agents_config[key])
        cfg["backstory"] = cfg["backstory"] + self._rules
        return cfg

    @agent
    def architect(self) -> Agent:
        return Agent(
            config=self._cfg("architect"),
            llm=llm_architect,
            tools=[dir_tool, self._file_read, git_log, mem_read],
            verbose=True
        )

    @agent
    def developer(self) -> Agent:
        return Agent(
            config=self._cfg("developer"),
            llm=llm_developer,
            tools=[dir_tool, self._file_read, file_write, py_validator, ts_validator, mem_write],
            verbose=True
        )

    @agent
    def reviewer(self) -> Agent:
        return Agent(
            config=self._cfg("reviewer"),
            llm=llm_reviewer,
            tools=[dir_tool, self._file_read, test_runner, mem_write],
            verbose=True
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
        # Only design phase — approval is handled manually after this
        return Crew(
            agents=[self.architect()],
            tasks=[self.design_task()],
            process=Process.sequential,
            verbose=True
        )

    def implement_crew(self) -> Crew:
        return Crew(
            agents=[self.developer()],
            tasks=[self.implement_task()],
            process=Process.sequential,
            verbose=True
        )

    def review_crew(self) -> Crew:
        return Crew(
            agents=[self.reviewer()],
            tasks=[self.review_task()],
            process=Process.sequential,
            verbose=True
        )

    def fix_crew(self) -> Crew:
        return Crew(
            agents=[self.developer(), self.reviewer()],
            tasks=[self.fix_task(), self.review_task()],
            process=Process.sequential,
            verbose=True
        )
