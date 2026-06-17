import difflib
import os
import re
import py_compile
import tempfile
from crewai.tools import BaseTool
from pydantic import BaseModel, Field


def _python_defs(content: str) -> set[str]:
    names = re.findall(r'^(?:async\s+)?def\s+(\w+)', content, re.MULTILINE)
    names += re.findall(r'^class\s+(\w+)', content, re.MULTILINE)
    return set(names)


def _normalize_lines(text: str) -> str:
    """Strip trailing whitespace per line, normalise line endings."""
    return "\n".join(line.rstrip() for line in text.splitlines())


class PatchFileInput(BaseModel):
    filename: str = Field(description="Full absolute path of the file to patch")
    old_snippet: str = Field(
        description=(
            "Exact text to find in the file. Must match verbatim — copy it "
            "character-for-character from what read_file returned. Include enough "
            "surrounding lines (at least 2 above and 2 below the change) to be unique."
        )
    )
    new_snippet: str = Field(
        description="Replacement text. Preserve indentation exactly."
    )


class PatchFileTool(BaseTool):
    name: str = "patch_file"
    description: str = (
        "Replace an exact snippet inside an existing file. "
        "ALWAYS prefer this over write_file for modifying existing files — "
        "it only touches the lines you specify and leaves the rest untouched. "
        "old_snippet must match the file EXACTLY (copy from read_file output). "
        "Include 2-3 lines of context above and below the changed section so the match is unique."
    )
    args_schema: type[BaseModel] = PatchFileInput
    project_path: str = ""

    def _run(self, filename: str, old_snippet: str, new_snippet: str) -> str:
        # ── Path validation ────────────────────────────────────────────────
        resolved = os.path.normpath(os.path.abspath(filename))
        if self.project_path:
            proj = os.path.normpath(os.path.abspath(self.project_path))
            if not resolved.startswith(proj + os.sep) and resolved != proj:
                candidate = os.path.normpath(os.path.join(proj, filename))
                if candidate.startswith(proj + os.sep):
                    resolved = candidate
                else:
                    return (
                        f"ERRO: '{filename}' está fora do projecto '{proj}'.\n"
                        "Usa o caminho absoluto completo dentro do projecto."
                    )
        filename = resolved

        if not os.path.exists(filename):
            return (
                f"ERRO: '{filename}' não existe. "
                "Para criar um ficheiro novo usa write_file, não patch_file."
            )

        with open(filename, encoding="utf-8") as f:
            original = f.read()

        # ── Find old_snippet (exact, then line-normalised) ─────────────────
        if old_snippet in original:
            patched = original.replace(old_snippet, new_snippet, 1)
            occurrences = original.count(old_snippet)
        else:
            # Try normalised comparison
            norm_original = _normalize_lines(original)
            norm_snippet  = _normalize_lines(old_snippet)
            if norm_snippet in norm_original:
                # Rebuild: find start position in normalised, map back
                start = norm_original.index(norm_snippet)
                end   = start + len(norm_snippet)
                patched = norm_original[:start] + _normalize_lines(new_snippet) + norm_original[end:]
                occurrences = norm_original.count(norm_snippet)
                original = norm_original  # diff against normalised
            else:
                # Give helpful context about what's close
                snippet_first_line = old_snippet.strip().splitlines()[0].strip()
                hints = [
                    ln for ln in original.splitlines()
                    if snippet_first_line[:30] in ln
                ]
                hint_str = (
                    ("\nLinhas semelhantes encontradas:\n  " + "\n  ".join(hints[:5]))
                    if hints else "\nNenhuma linha semelhante encontrada."
                )
                return (
                    f"ERRO: old_snippet não foi encontrado em '{os.path.basename(filename)}'.\n"
                    "Isto significa que o texto que forneceste NÃO é exactamente igual ao ficheiro.\n"
                    "Solução: usa read_file para ler o ficheiro novamente e copia o trecho "
                    f"LITERALMENTE, incluindo espaços e indentação.{hint_str}"
                )

        if occurrences > 1:
            return (
                f"ERRO: old_snippet aparece {occurrences} vezes no ficheiro — é ambíguo.\n"
                "Adiciona mais linhas de contexto (acima/abaixo) para tornar o trecho único."
            )

        # ── Python: validate syntax + def preservation ─────────────────────
        if filename.endswith(".py"):
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(patched)
                tmp_path = tmp.name
            try:
                py_compile.compile(tmp_path, doraise=True)
            except py_compile.PyCompileError as e:
                os.unlink(tmp_path)
                return (
                    f"🛑 BLOQUEADO — o patch introduz erro de sintaxe Python:\n"
                    f"{str(e).replace(tmp_path, filename)}\nCorrige new_snippet antes de aplicar."
                )
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

            missing = _python_defs(original) - _python_defs(patched)
            if missing:
                return (
                    "🛑 BLOQUEADO — o patch remove funções/classes que existem no original:\n"
                    + "\n".join(f"  ✗ {d}" for d in sorted(missing))
                    + "\nVerifica new_snippet — não deves apagar estas definições."
                )

        # ── Show diff ──────────────────────────────────────────────────────
        diff = list(difflib.unified_diff(
            original.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=f"a/{os.path.basename(filename)}",
            tofile=f"b/{os.path.basename(filename)}",
            n=3,
        ))
        print("\n" + "=" * 60)
        print(f"  PATCH: {filename}")
        print("".join(diff[:80]))
        if len(diff) > 80:
            print(f"... (+{len(diff) - 80} linhas)")
        print("=" * 60)

        try:
            from dev_studio.api import capture as _cap
            dashboard_active = _cap._dashboard_active
        except Exception:
            dashboard_active = False

        if dashboard_active:
            # Running inside the Dev Studio dashboard — skip interactive prompt.
            # The agent has already shown the diff via stdout; auto-apply the patch.
            with open(filename, "w", encoding="utf-8") as f:
                f.write(patched)
            return f"✅ Patch aplicado em '{filename}' (modo dashboard)."

        answer = input("\n  Aprovas este patch? (s/n): ").strip().lower()
        if answer != "s":
            return f"Cancelado: '{filename}' não foi alterado."

        with open(filename, "w", encoding="utf-8") as f:
            f.write(patched)
        return f"✅ Patch aplicado em '{filename}'."
