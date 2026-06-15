import os
from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class ApprovalWriterInput(BaseModel):
    filename: str = Field(description="Full path of the file to write")
    content: str = Field(description="Content to write to the file")
    overwrite: bool = Field(default=True, description="Whether to overwrite if file exists")


class ApprovalFileWriterTool(BaseTool):
    name: str = "write_file"
    description: str = (
        "Write content to a file. Always asks for human approval before writing."
    )
    args_schema: type[BaseModel] = ApprovalWriterInput

    def _run(self, filename: str, content: str, overwrite: bool = True) -> str:
        print("\n" + "=" * 60)
        print(f"📝  O Developer quer escrever em: {filename}")
        print("=" * 60)
        preview = content if len(content) <= 800 else content[:800] + "\n... (truncado)"
        print(preview)
        print("=" * 60)

        if os.path.exists(filename) and not overwrite:
            print("⚠️  Ficheiro já existe e overwrite=False — será ignorado.")
            return f"Ignorado: '{filename}' já existe."

        answer = input(f"\n✅ Aprovas a escrita em '{filename}'? (s/n): ").strip().lower()
        if answer != "s":
            return f"❌ Cancelado pelo utilizador: '{filename}' não foi alterado."

        os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ Ficheiro '{filename}' escrito com sucesso."
