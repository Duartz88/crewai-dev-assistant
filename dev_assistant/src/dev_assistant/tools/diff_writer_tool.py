import difflib
import os
from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class DiffWriterInput(BaseModel):
    filename: str = Field(description="Full path of the file to write")
    content: str = Field(description="New content for the file")


class DiffFileWriterTool(BaseTool):
    name: str = "write_file"
    description: str = (
        "Write content to a file. Shows a diff for existing files and asks for "
        "human approval before writing."
    )
    args_schema: type[BaseModel] = DiffWriterInput

    def _run(self, filename: str, content: str) -> str:
        print("\n" + "=" * 60)
        if os.path.exists(filename):
            with open(filename, encoding="utf-8") as f:
                old = f.read()
            diff = list(difflib.unified_diff(
                old.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{os.path.basename(filename)}",
                tofile=f"b/{os.path.basename(filename)}",
                n=3
            ))
            if not diff:
                return f"ℹ️  Sem alterações em '{filename}' — ficheiro não modificado."
            print(f"📝  MODIFICAR: {filename}")
            print("".join(diff[:120]))
            if len(diff) > 120:
                print(f"... (+{len(diff) - 120} linhas)")
        else:
            print(f"📄  CRIAR: {filename}")
            preview = content if len(content) <= 600 else content[:600] + "\n... (truncado)"
            print(preview)
        print("=" * 60)

        answer = input(f"\n✅  Aprovas? (s/n): ").strip().lower()
        if answer != "s":
            return f"❌  Cancelado: '{filename}' não foi alterado."

        os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅  '{filename}' escrito com sucesso."
