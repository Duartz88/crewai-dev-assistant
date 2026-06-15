import os
import subprocess
import tempfile
from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class PowerShellValidatorInput(BaseModel):
    file_path: str = Field(default="", description="Absolute path to .ps1 file to validate")
    content: str = Field(default="", description="PowerShell script content to validate (alternative to file_path)")


class ValidatePowerShellTool(BaseTool):
    name: str = "validate_powershell_syntax"
    description: str = (
        "Validate PowerShell script syntax using the PowerShell parser. "
        "Detects: invalid parameters, syntax errors, missing brackets, "
        "malformed expressions, and other parse-time issues. "
        "Does NOT execute the script — static analysis only. "
        "Provide either file_path (path to .ps1 file) or content (script text)."
    )
    args_schema: type[BaseModel] = PowerShellValidatorInput

    def _run(self, file_path: str = "", content: str = "") -> str:
        tmp_path: str | None = None

        if content and not file_path:
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".ps1", delete=False, encoding="utf-8"
                ) as f:
                    f.write(content)
                    tmp_path = f.name
                check_path = tmp_path
            except Exception as e:
                return f"Erro ao criar ficheiro temporário: {e}"
        elif file_path:
            check_path = file_path
        else:
            return "ERRO: Fornece file_path ou content."

        if not os.path.exists(check_path):
            return f"Ficheiro não encontrado: '{check_path}'"

        # Escape single quotes for embedding in PS string
        safe_path = check_path.replace("\\", "\\\\").replace("'", "\\'")

        ps_script = f"""
$errList = [System.Collections.Generic.List[System.Management.Automation.Language.ParseError]]::new()
$tokList = [System.Collections.Generic.List[System.Management.Automation.Language.Token]]::new()
$null = [System.Management.Automation.Language.Parser]::ParseFile('{safe_path}', [ref]$tokList, [ref]$errList)
if ($errList.Count -eq 0) {{
    Write-Output "SYNTAX OK: Nenhum erro de sintaxe PowerShell detectado."
}} else {{
    Write-Output "ERROS DE SINTAXE ENCONTRADOS ($($errList.Count)):"
    foreach ($e in $errList) {{
        Write-Output "  Linha $($e.Extent.StartLineNumber), Coluna $($e.Extent.StartColumnNumber): $($e.Message)"
        Write-Output "  Codigo: $($e.Extent.Text)"
    }}
}}
"""
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True, text=True, timeout=30, encoding="utf-8",
                errors="replace",
            )
            out = (result.stdout or "").strip()
            err = (result.stderr or "").strip()
            if out:
                return out
            if err:
                return f"PowerShell stderr:\n{err}"
            return "Sem output do validador PowerShell."
        except FileNotFoundError:
            return (
                "AVISO: powershell.exe não encontrado no PATH. "
                "Validação automática de sintaxe PS1 não disponível neste ambiente."
            )
        except subprocess.TimeoutExpired:
            return "ERRO: Timeout ao validar sintaxe PowerShell (>30s)."
        except Exception as e:
            return f"Erro ao invocar PowerShell: {e}"
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
