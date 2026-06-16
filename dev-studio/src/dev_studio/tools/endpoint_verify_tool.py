import os
import re
from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class EndpointVerifyInput(BaseModel):
    endpoint: str = Field(
        description=(
            'Endpoint a verificar, formato "MÉTODO /caminho/completo". '
            'Exemplos: "GET /points/{member_id}", "POST /rewards/redeem", "PATCH /tasks/{id}"'
        )
    )
    file_path: str = Field(
        description=(
            "Caminho do ficheiro do router onde o endpoint deve estar. "
            "Exemplos: api/app/routers/points.py, api/app/routers/rewards.py"
        )
    )


class EndpointVerifyTool(BaseTool):
    name: str = "mark_endpoint_verified"
    description: str = (
        "OBRIGATÓRIO antes de incluir qualquer endpoint da API no plano. "
        "Lê o ficheiro do router especificado, confirma que o endpoint existe, "
        "e devolve a assinatura real (parâmetros, modelo de resposta). "
        "Se o endpoint não for encontrado, lista todos os disponíveis nesse router. "
        "NUNCA incluas um endpoint no plano sem teres chamado esta ferramenta — "
        "o plano será rejeitado pelo reviewer se não houver verificação."
    )
    args_schema: type[BaseModel] = EndpointVerifyInput
    project_path: str = ""

    def _run(self, endpoint: str, file_path: str) -> str:
        parts = endpoint.strip().split(" ", 1)
        if len(parts) != 2:
            return (
                "Erro: formato inválido. Usa 'MÉTODO /caminho', "
                "ex: 'GET /points/{member_id}'"
            )

        method = parts[0].upper()
        path = parts[1].strip()
        method_lower = method.lower()

        resolved = self._resolve(file_path)
        if not os.path.exists(resolved):
            alt = resolved.replace("/", os.sep).replace("\\", os.sep)
            resolved = alt if os.path.exists(alt) else resolved
        if not os.path.exists(resolved):
            return (
                f"Erro: ficheiro não encontrado em '{resolved}'. "
                f"Verifica o caminho e usa list_project_structure para confirmar."
            )

        with open(resolved, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        router_prefix = self._extract_router_prefix(lines)
        path_variants = self._path_variants(path, router_prefix)

        # Search for a matching decorator
        for i, line in enumerate(lines):
            if not re.search(
                r'@(router|app)\.' + re.escape(method_lower) + r'\s*\(',
                line, re.IGNORECASE
            ):
                continue
            for variant in path_variants:
                if variant in line:
                    block = self._extract_block(lines, i)
                    result = f"✅ Endpoint verificado: {endpoint}\n"
                    result += f"Ficheiro: {file_path}"
                    if router_prefix:
                        result += f" (prefix do router: '{router_prefix}')"
                    result += f" | Linha: {i + 1}\n\n"
                    result += "Assinatura encontrada:\n```python\n"
                    result += "".join(block)
                    result += "```"
                    return result

        # Not found — list all routes in this file
        all_routes = self._list_all_routes(lines, router_prefix)
        result = f"❌ Endpoint '{endpoint}' não encontrado em {file_path}.\n"
        if router_prefix:
            result += f"Prefix do router: '{router_prefix}'\n"
        if all_routes:
            result += "\nEndpoints disponíveis neste router:\n"
            for r in all_routes:
                result += f"  {r}\n"
        else:
            result += "\nNenhum endpoint encontrado neste ficheiro.\n"
        result += (
            "\nSe o endpoint não existe, terás de o criar. "
            "Relê o router com read_file e corrige o plano."
        )
        return result

    # ── helpers ────────────────────────────────────────────────────────────────

    def _extract_router_prefix(self, lines: list[str]) -> str:
        for line in lines:
            m = re.search(
                r'APIRouter\s*\(.*?prefix\s*=\s*["\']([^"\']+)["\']', line
            )
            if m:
                return m.group(1)
        return ""

    def _path_variants(self, path: str, router_prefix: str) -> list[str]:
        variants = [path]
        if router_prefix and path.startswith(router_prefix):
            suffix = path[len(router_prefix):]
            variants.append(suffix or "/")
        parts = path.strip("/").split("/")
        if len(parts) > 1:
            variants.append("/" + "/".join(parts[1:]))
            variants.append("/" + parts[-1])
        # Empty string matches root ("")
        if path in ("", "/"):
            variants.extend(["\"\"", "''", '""'])
        return variants

    def _extract_block(self, lines: list[str], decorator_idx: int) -> list[str]:
        block: list[str] = []
        i = decorator_idx
        # Collect full decorator (may span multiple lines)
        while i < len(lines):
            block.append(lines[i])
            if ")" in lines[i]:
                i += 1
                break
            i += 1
        # Collect function signature
        depth = 0
        while i < len(lines):
            block.append(lines[i])
            depth += lines[i].count("(") - lines[i].count(")")
            if ("async def " in lines[i] or "def " in lines[i]) and depth <= 0:
                i += 1
                break
            i += 1
        # Add up to 3 more lines (first statements)
        for _ in range(3):
            if i < len(lines) and lines[i].strip():
                block.append(lines[i])
                i += 1
        return block[:20]  # hard cap

    def _list_all_routes(self, lines: list[str], prefix: str) -> list[str]:
        routes: list[str] = []
        methods = ["get", "post", "put", "patch", "delete"]
        for i, line in enumerate(lines):
            for m in methods:
                if re.search(
                    r'@(router|app)\.' + m + r'\s*\(', line, re.IGNORECASE
                ):
                    path_m = re.search(r'["\']([^"\']*)["\']', line)
                    suffix = path_m.group(1) if path_m else "?"
                    full = (prefix + suffix) if (prefix and not suffix.startswith(prefix)) else suffix
                    func = "?"
                    if i + 1 < len(lines):
                        fn_m = re.search(r'(?:async )?def\s+(\w+)', lines[i + 1])
                        if fn_m:
                            func = fn_m.group(1)
                    routes.append(f"{m.upper()} {full or '/'} → {func}()  (linha {i + 1})")
                    break
        return routes

    def _resolve(self, file_path: str) -> str:
        if os.path.isabs(file_path):
            return file_path
        fp = file_path.replace("/", os.sep)
        return os.path.join(self.project_path, fp) if self.project_path else fp
