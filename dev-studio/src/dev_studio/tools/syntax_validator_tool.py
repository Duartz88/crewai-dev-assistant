import ast
import py_compile
import re
import tempfile
import os
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

# Padrões que passam no py_compile mas são erros comuns do LLM
_SEMANTIC_PATTERNS = [
    (r"\b\w+/\w+\(", "Possível erro: '/' usado em vez de '.' numa chamada (ex: requests/get → requests.get)"),
    (r'["\']D:[/\\\\]', "Path hardcoded no código — usa variável de ambiente ou argumento"),
    (r'["\']C:[/\\\\]', "Path hardcoded no código — usa variável de ambiente ou argumento"),
]


class SyntaxValidatorInput(BaseModel):
    filename: str = Field(description="Path of the file to validate")
    content: str = Field(description="Python source code to validate")


class PythonSyntaxValidatorTool(BaseTool):
    name: str = "validate_python_syntax"
    description: str = (
        "Validate Python syntax of code before writing it to disk. "
        "Always use this before write_file for any .py file."
    )
    args_schema: type[BaseModel] = SyntaxValidatorInput

    def _run(self, filename: str, content: str) -> str:
        if not filename.endswith(".py"):
            return "Not a Python file — skipping syntax check."
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                         delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            py_compile.compile(tmp_path, doraise=True)
        except py_compile.PyCompileError as e:
            error = str(e).replace(tmp_path, filename)
            return f"❌ Erro de sintaxe em '{filename}':\n{error}\nCorrige antes de escrever."
        finally:
            os.unlink(tmp_path)

        # Verificações semânticas
        warnings = []
        for i, line in enumerate(content.splitlines(), 1):
            for pattern, msg in _SEMANTIC_PATTERNS:
                if re.search(pattern, line):
                    warnings.append(f"  Linha {i}: {msg}\n    → {line.strip()}")

        if warnings:
            return (
                f"⚠️  Sintaxe válida mas problemas detetados em '{filename}':\n"
                + "\n".join(warnings)
                + "\nCorrige antes de escrever."
            )
        return f"✅ Validação completa: '{filename}' pode ser escrito."
