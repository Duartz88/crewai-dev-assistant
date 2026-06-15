# Regras Globais — Dev Studio

Aplicam-se a TODOS os projetos, sempre. Prioridade decrescente.

## 🔴 Crítico (nunca violar)
- Faz APENAS o que foi pedido — sem extras, melhorias ou refactorings não solicitados
- Lê SEMPRE o ficheiro completo antes de o modificar
- Nunca apagues endpoints, funções ou classes existentes sem pedido explícito
- Nunca cries diretórios de topo (backend/, frontend/, docker/, etc.) sem pedido
- Sem credenciais, tokens ou secrets no código

## 🟡 Importante (respeitar sempre)
- Python: imports com pontos — `from app.models import X` (nunca barras)
- Python: valida sempre com validate_python_syntax antes de write_file
- TypeScript: sem `any` implícito — tipos explícitos obrigatórios
- Sem paths hardcoded (C:/, D:/, /home/user/...) — usa variáveis ou argumentos

## 🟢 Qualidade
- Sem console.log() ou print() de debug esquecidos
- Sem comentários que explicam o óbvio
- Segue os padrões de código já existentes no projeto (indentação, naming, etc.)
