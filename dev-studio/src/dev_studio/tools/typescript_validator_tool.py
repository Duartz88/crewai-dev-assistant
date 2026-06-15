import os
import re
import subprocess
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

_PATTERNS = [
    (r"\bany\b", "Uso de 'any' explícito — define um tipo concreto"),
    (r"console\.log\(", "console.log() em código de produção"),
    (r"@ts-ignore", "@ts-ignore suprime erros de tipo sem os corrigir"),
    (r"require\(['\"]\.{1,2}/", "require() em vez de import — prefere ES modules"),
]


class TypeScriptValidatorInput(BaseModel):
    filename: str = Field(description="Path of the TypeScript file to validate")
    content: str = Field(description="TypeScript source code to validate")


class TypeScriptValidatorTool(BaseTool):
    name: str = "validate_typescript"
    description: str = (
        "Validate TypeScript syntax and common issues before writing a .ts or .tsx file. "
        "Use this before write_file for any TypeScript file."
    )
    args_schema: type[BaseModel] = TypeScriptValidatorInput

    def _run(self, filename: str, content: str) -> str:
        if not filename.endswith((".ts", ".tsx")):
            return "Not a TypeScript file — skipping."

        # Try tsc if available
        tsc_result = self._run_tsc(filename, content)
        if tsc_result:
            return tsc_result

        # Fallback: regex pattern checks
        warnings = []
        for i, line in enumerate(content.splitlines(), 1):
            for pattern, msg in _PATTERNS:
                if re.search(pattern, line):
                    warnings.append(f"  Linha {i}: {msg}\n    → {line.strip()}")

        if warnings:
            return (
                f"⚠️  Avisos em '{filename}':\n" + "\n".join(warnings)
                + "\nConsidera corrigir antes de escrever."
            )
        return f"✅  TypeScript validado: '{filename}' pode ser escrito."

    def _run_tsc(self, filename: str, content: str) -> str | None:
        import tempfile
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".ts", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            result = subprocess.run(
                ["tsc", "--noEmit", "--allowJs", "--target", "ES2020", tmp_path],
                capture_output=True, text=True, timeout=15
            )
            os.unlink(tmp_path)
            if result.returncode != 0:
                errors = result.stdout.replace(tmp_path, filename)
                return f"❌  Erros TypeScript em '{filename}':\n{errors}"
            return None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None  # tsc not available, fall through to regex
