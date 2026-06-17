import os
from datetime import datetime
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

MEMORY_FILE = "CREW_MEMORY.md"


class MemoryReadInput(BaseModel):
    project_path: str = Field(description="Root path of the project")


class MemoryWriteInput(BaseModel):
    project_path: str = Field(description="Root path of the project")
    entry: str = Field(description="Decision or learning to record (1-3 sentences)")
    category: str = Field(
        default="Geral",
        description="Category: Decisão, Padrão, Problema, Aprendizagem"
    )


class ProjectMemoryReadTool(BaseTool):
    name: str = "read_project_memory"
    description: str = (
        "Read past decisions and learnings about this project recorded by previous crew runs. "
        "Always call this at the start to avoid repeating past mistakes."
    )
    args_schema: type[BaseModel] = MemoryReadInput

    def _run(self, project_path: str) -> str:
        path = os.path.join(project_path, MEMORY_FILE)
        if not os.path.exists(path):
            return "ℹ️  Sem memória registada para este projeto ainda."
        with open(path, encoding="utf-8") as f:
            return f.read()


class ProjectMemoryWriteTool(BaseTool):
    name: str = "write_project_memory"
    description: str = (
        "Record a decision, pattern, or learning about this project for future crew runs. "
        "Use this when you discover something important about the project structure, "
        "a constraint, or a decision made with the user."
    )
    args_schema: type[BaseModel] = MemoryWriteInput

    def _run(self, project_path: str, entry: str, category: str = "Geral") -> str:
        path = os.path.join(project_path, MEMORY_FILE)
        date = datetime.now().strftime("%Y-%m-%d %H:%M")
        line = f"\n- **[{category}]** ({date}): {entry}"
        needs_header = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if needs_header:
                f.write("# Memória do Projeto (CrewAI)\n")
            f.write(line + "\n")
        suffix = "..." if len(entry) > 60 else ""
        return f"✅  Memória registada: {entry[:60]}{suffix}"
