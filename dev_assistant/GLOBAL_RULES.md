# Regras Globais do Dev Assistant

Estas regras aplicam-se a TODOS os projetos, sempre.

## Scope e disciplina
- Faz APENAS o que foi pedido — sem melhorias, refactorings ou extras não solicitados
- Não cries diretórios de topo (ex: backend/, infrastructure/, frontend/, docker/) sem pedido explícito
- Não modifiques ficheiros fora do scope aprovado pelo utilizador (ex: .gitignore, package.json)
- Se o pedido for ambíguo, inclui no plano uma pergunta clara ao utilizador

## Preservação de código existente
- Nunca apagues endpoints, funções, classes ou métodos existentes
- Ao modificar um ficheiro, lê o conteúdo COMPLETO antes de o reescrever
- Só adiciona ou altera — nunca remove funcionalidade sem pedido explícito

## Python
- Imports sempre com pontos: `from app.models.finance import X`
- Nunca barras: `from app/models/finance import X` — ERRADO
- Chamadas de módulo com pontos: `requests.get(url)` não `requests/get(url)`
- Valida sempre a sintaxe com validate_python_syntax antes de escrever ficheiros .py

## TypeScript / JavaScript
- Sem `any` implícito — usa tipos explícitos
- Imports relativos com `./` ou `../` conforme a estrutura do projeto

## Qualidade geral
- Sem paths hardcoded (ex: `D:/_DEV/Projects/...`) — usa variáveis ou argumentos
- Sem credenciais, tokens ou secrets no código
- Sem comentários que expliquem o óbvio — só comentários com valor real
