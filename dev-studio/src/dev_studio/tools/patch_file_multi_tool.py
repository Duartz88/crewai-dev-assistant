import difflib
import os
from crewai.tools import BaseTool
from pydantic import BaseModel, Field


def _normalize_lines(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines())


def _find_stripped_match(content: str, snippet: str) -> str | None:
    """Find snippet by comparing stripped lines (ignores leading/trailing whitespace).

    Returns the actual file text or None if not found/ambiguous.
    """
    c_lines = content.splitlines(keepends=True)
    s_lines = snippet.splitlines()
    n = len(s_lines)
    if n == 0:
        return None
    c_sigs = [l.strip() for l in c_lines]
    s_sigs = [l.strip() for l in s_lines]
    matches: list[tuple[int, int]] = []
    for i in range(len(c_lines) - n + 1):
        if c_sigs[i:i + n] == s_sigs:
            matches.append((i, i + n))
    if len(matches) == 1:
        return "".join(c_lines[matches[0][0]:matches[0][1]])
    return None


class PatchEntry(BaseModel):
    old_snippet: str = Field(description="Exact text to find (copy verbatim from read_file or plan ANTES block)")
    new_snippet: str = Field(description="Replacement text (preserve indentation)")


class PatchFileMultiInput(BaseModel):
    filename: str = Field(description="Full absolute path of the file to patch")
    patches: list[PatchEntry] = Field(
        description=(
            "List of {old_snippet, new_snippet} pairs to apply in order. "
            "All patches are applied to the same file in a single write. "
            "If any patch fails, no changes are written."
        )
    )


class PatchFileMultiTool(BaseTool):
    name: str = "patch_file_multi"
    description: str = (
        "Apply multiple targeted patches to a single file in one operation. "
        "Each patch replaces one old_snippet with new_snippet — surgical, no full rewrite. "
        "All patches are validated first; the file is written only if ALL succeed. "
        "Use this instead of calling patch_file repeatedly for the same file."
    )
    args_schema: type[BaseModel] = PatchFileMultiInput
    project_path: str = ""

    def _run(self, filename: str, patches: list[PatchEntry] | list[dict]) -> str:
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
                "Para criar um ficheiro novo usa write_file, não patch_file_multi."
            )

        # Normalise patches — accept both PatchEntry objects and raw dicts
        patch_list: list[tuple[str, str]] = []
        for p in patches:
            if isinstance(p, dict):
                patch_list.append((p["old_snippet"], p["new_snippet"]))
            else:
                patch_list.append((p.old_snippet, p.new_snippet))

        with open(filename, encoding="utf-8") as f:
            content = f.read()

        # ── Apply each patch sequentially, accumulating into content ──────
        errors: list[str] = []
        for idx, (old, new) in enumerate(patch_list, 1):
            if old in content:
                occurrences = content.count(old)
                if occurrences > 1:
                    errors.append(
                        f"Patch #{idx}: old_snippet aparece {occurrences} vezes — ambíguo. "
                        "Adiciona mais contexto para tornar o trecho único."
                    )
                    continue
                content = content.replace(old, new, 1)
            else:
                norm_content = _normalize_lines(content)
                norm_old     = _normalize_lines(old)
                if norm_old in norm_content:
                    occ = norm_content.count(norm_old)
                    if occ > 1:
                        errors.append(
                            f"Patch #{idx}: old_snippet aparece {occ} vezes (normalizado) — ambíguo."
                        )
                        continue
                    start   = norm_content.index(norm_old)
                    end     = start + len(norm_old)
                    content = norm_content[:start] + _normalize_lines(new) + norm_content[end:]
                else:
                    # Try stripped-line matching (ignores leading/trailing whitespace per line)
                    actual_old = _find_stripped_match(content, old)
                    if actual_old is not None:
                        occ = content.count(actual_old)
                        if occ > 1:
                            errors.append(
                                f"Patch #{idx}: correspondência aproximada ambígua ({occ}×)."
                            )
                        else:
                            content = content.replace(actual_old, new, 1)
                    else:
                        first_line = old.strip().splitlines()[0].strip()[:40]
                        hints = [ln for ln in content.splitlines() if first_line[:20] in ln]
                        hint_str = (
                            "\n  Linhas semelhantes:\n    " + "\n    ".join(hints[:3])
                            if hints else "\n  Nenhuma linha semelhante encontrada."
                        )
                        errors.append(
                            f"Patch #{idx}: old_snippet não encontrado.{hint_str}\n"
                            "  → Copia o trecho LITERALMENTE do ficheiro (read_file) ou do bloco ANTES do plano."
                        )

        if errors:
            return (
                f"⚠️ {len(errors)} patch(es) falharam — ficheiro NÃO foi alterado:\n\n"
                + "\n\n".join(errors)
                + "\n\nCorrige os old_snippets e tenta novamente."
            )

        # ── Show consolidated diff ─────────────────────────────────────────
        with open(filename, encoding="utf-8") as f:
            original = f.read()

        diff = list(difflib.unified_diff(
            original.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=f"a/{os.path.basename(filename)}",
            tofile=f"b/{os.path.basename(filename)}",
            n=2,
        ))
        print("\n" + "=" * 60)
        print(f"  PATCH MULTI ({len(patch_list)} alterações): {filename}")
        print("".join(diff[:120]))
        if len(diff) > 120:
            print(f"... (+{len(diff) - 120} linhas)")
        print("=" * 60)

        # ── Write once ────────────────────────────────────────────────────
        try:
            from dev_studio.api import capture as _cap
            dashboard_active = _cap._dashboard_active
        except Exception:
            dashboard_active = False

        if not dashboard_active:
            answer = input("\n  Aprovas estes patches? (s/n): ").strip().lower()
            if answer != "s":
                return f"Cancelado: '{filename}' não foi alterado."

        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)

        return (
            f"✅ {len(patch_list)} patch(es) aplicados em '{os.path.basename(filename)}' "
            f"(modo dashboard)." if dashboard_active else
            f"✅ {len(patch_list)} patch(es) aplicados em '{filename}'."
        )
