import difflib
import os
import re
from crewai.tools import BaseTool
from pydantic import BaseModel, Field


def _python_defs(content: str) -> set[str]:
    """Return all top-level def/class names."""
    names = re.findall(r'^(?:async\s+)?def\s+(\w+)', content, re.MULTILINE)
    names += re.findall(r'^class\s+(\w+)', content, re.MULTILINE)
    return set(names)


class DiffWriterInput(BaseModel):
    filename: str = Field(description="Full absolute path of the file to write")
    content: str = Field(description="New content for the file")


class DiffFileWriterTool(BaseTool):
    name: str = "write_file"
    description: str = (
        "Write content to a file. Shows a diff for existing files and asks for "
        "human approval before writing. The filename MUST be an absolute path "
        "inside the project directory."
    )
    args_schema: type[BaseModel] = DiffWriterInput
    project_path: str = ""

    def _run(self, filename: str, content: str) -> str:
        # ── Normalize and validate path ────────────────────────────────────
        resolved = os.path.normpath(os.path.abspath(filename))

        if self.project_path:
            proj = os.path.normpath(os.path.abspath(self.project_path))

            # If path falls outside the project, try resolving it as relative
            if not resolved.startswith(proj + os.sep) and resolved != proj:
                candidate = os.path.normpath(os.path.join(proj, filename))
                if candidate.startswith(proj + os.sep):
                    resolved = candidate
                else:
                    return (
                        f"ERRO: O caminho '{filename}' está fora do projecto.\n"
                        f"Todos os ficheiros devem estar dentro de: '{proj}'\n"
                        f"Corrige o caminho e tenta novamente."
                    )

        filename = resolved

        # ── Safety checks for existing files ──────────────────────────────
        old = None
        if os.path.exists(filename):
            with open(filename, encoding="utf-8") as f:
                old = f.read()

            old_lines = old.splitlines()
            new_lines = content.splitlines()

            # Block if new content is suspiciously shorter (>25% fewer lines)
            if old_lines and len(new_lines) < len(old_lines) * 0.75:
                removed = len(old_lines) - len(new_lines)
                pct = int(100 * removed / len(old_lines))
                return (
                    f"🛑 BLOQUEADO — o novo conteúdo tem {len(new_lines)} linhas mas "
                    f"o original tem {len(old_lines)} ({pct}% de redução, {removed} linhas a menos).\n"
                    f"Isto é muito suspeito. Volta a ler o ficheiro com read_file e certifica-te "
                    f"que TODAS as funções/endpoints existentes estão incluídos no novo conteúdo."
                )

            # Block if Python defs/classes went missing
            if filename.endswith(".py"):
                missing = _python_defs(old) - _python_defs(content)
                if missing:
                    return (
                        f"🛑 BLOQUEADO — as seguintes funções/classes do original desapareceram "
                        f"do novo conteúdo:\n"
                        + "\n".join(f"  ✗ {d}" for d in sorted(missing))
                        + "\n\nVerifica o novo conteúdo e inclui TODAS as funções existentes."
                    )

        # ── Show diff / preview ────────────────────────────────────────────
        print("\n" + "=" * 60)
        if old is not None:
            diff = list(difflib.unified_diff(
                old.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{os.path.basename(filename)}",
                tofile=f"b/{os.path.basename(filename)}",
                n=3,
            ))
            if not diff:
                return f"Sem alterações em '{filename}' — ficheiro não modificado."
            print(f"  MODIFICAR: {filename}")
            print("".join(diff[:120]))
            if len(diff) > 120:
                print(f"... (+{len(diff) - 120} linhas)")
        else:
            print(f"  CRIAR: {filename}")
            preview = content if len(content) <= 600 else content[:600] + "\n... (truncado)"
            print(preview)
        print("=" * 60)

        answer = input(f"\n  Aprovas? (s/n): ").strip().lower()
        if answer != "s":
            return f"Cancelado: '{filename}' não foi alterado."

        os.makedirs(os.path.dirname(filename), exist_ok=True)
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        return f"'{filename}' escrito com sucesso."
