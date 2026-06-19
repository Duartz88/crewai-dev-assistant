import os
import sys
import time
from datetime import datetime

from dev_studio.utils.env_loader import load_env
load_env()  # must run before any crewai import

from rich.console import Console  # noqa: E402
from rich.panel import Panel      # noqa: E402
from rich.table import Table      # noqa: E402
from dev_studio.crew import DevStudioCrew  # noqa: E402
from dev_studio.utils.git_utils import run_git, create_session_branch  # noqa: E402

console = Console()


# ── Git helpers ──────────────────────────────────────────────────────────────

def _create_session_branch(project_path: str) -> str | None:
    branch, error = create_session_branch(project_path)
    if branch:
        console.print(f"[green]🌿  Branch criada:[/green] {branch}")
    elif error and error != "Not a git repository":
        console.print(f"[yellow]⚠️   Não foi possível criar branch:[/yellow] {error}")
    return branch


def _commit_session(project_path: str, branch: str | None, requests: list[dict]):
    diff = run_git(["diff", "--stat"], project_path)
    untracked = run_git(["ls-files", "--others", "--exclude-standard"], project_path)

    if not diff.stdout.strip() and not untracked.stdout.strip():
        console.print("\n[dim]Sem alterações para commitar.[/dim]")
        return

    console.print("\n" + diff.stdout.strip())
    answer = console.input("\n[bold]💾  Fazer commit da sessão? (s/n):[/bold] ").strip().lower()
    if answer != "s":
        if branch:
            console.print(f"[dim]Alterações mantidas na branch [italic]{branch}[/italic][/dim]")
        return

    # Build commit message summarising all requests
    summary_lines = "\n".join(f"- {r['request'][:80]}" for r in requests)
    default_msg = f"crew session: {len(requests)} pedido{'s' if len(requests) != 1 else ''} completado{'s' if len(requests) != 1 else ''}"
    msg = console.input(f"[bold]Mensagem [[dim]{default_msg}[/dim]]:[/bold] ").strip() or default_msg
    full_msg = f"{msg}\n\n{summary_lines}"

    run_git(["add", "-A"], project_path)
    result = run_git(["commit", "-m", full_msg], project_path)
    if result.returncode == 0:
        console.print(f"[green]✅  Commit feito na branch [italic]{branch}[/italic][/green]")
    else:
        console.print(f"[red]❌  Erro no commit:[/red] {result.stderr.strip()}")


# ── Fix cycle ─────────────────────────────────────────────────────────────────

def _offer_fix_cycle(result, inputs: dict):
    raw = str(result.raw) if hasattr(result, "raw") else str(result)
    if "Requer correções" not in raw and "❌" not in raw:
        return
    console.print("\n[yellow]🔧  Reviewer encontrou problemas.[/yellow]")
    answer = console.input("[bold]Iniciar ciclo de correção? (s/n):[/bold] ").strip().lower()
    if answer != "s":
        return
    fix_inputs = {**inputs, "review_feedback": raw}
    console.print("\n[cyan]🔄  A iniciar ciclo de correção...[/cyan]\n")
    DevStudioCrew(project_path=inputs.get("project_path", "")).fix_crew().kickoff(inputs=fix_inputs)  # type: ignore[attr-defined]


# ── Session summary ──────────────────────────────────────────────────────────

def _print_summary(requests: list[dict], total_elapsed: float):
    console.print()
    console.rule("[bold]Resumo da sessão[/bold]")
    if not requests:
        console.print("[dim]Nenhum pedido completado.[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("#", width=3)
    table.add_column("Pedido")
    table.add_column("Tempo", width=8)
    table.add_column("Estado", width=10)

    for r in requests:
        elapsed_str = f"{r['elapsed']:.0f}s"
        table.add_row(str(r["num"]), r["request"][:60], elapsed_str, r["status"])

    console.print(table)
    console.print(f"\n[bold]Total:[/bold] {len(requests)} pedidos | {total_elapsed:.0f}s")


# ── Main session loop ─────────────────────────────────────────────────────────

def run():
    # ── Setup (once per session) ──
    default_project = os.environ.get("TARGET_PROJECT", "")
    if default_project:
        project_path = default_project
    else:
        project_path = console.input("[bold]Projeto:[/bold] ").strip()

    console.print(Panel(
        f"[bold]Dev Studio[/bold]\n"
        f"[dim]Projeto:[/dim] {project_path}\n"
        f"[dim]Iniciado:[/dim] {datetime.now().strftime('%H:%M:%S')}",
        border_style="cyan"
    ))

    branch = _create_session_branch(project_path)

    session_requests: list[dict] = []
    session_start = time.time()
    request_num = 0

    while True:
        request_num += 1
        console.rule(f"[bold cyan]Pedido #{request_num}[/bold cyan]")

        feature_request = console.input("[bold]🎯  Pedido[/bold] [dim](ou 'sair')[/dim]: ").strip()

        if feature_request.lower() in ("sair", "exit", "q", ""):
            request_num -= 1
            break

        base_inputs = {
            "feature_request": feature_request,
            "project_path": project_path,
            "architect_plan": "",
            "review_feedback": "",
        }

        start = time.time()
        try:
            crew = DevStudioCrew(project_path=project_path)

            console.print("\n[purple]🏗️  FASE 1/3 — Architect[/purple]")
            design_result = crew.crew().kickoff(inputs=base_inputs)
            architect_plan = str(design_result.raw) if hasattr(design_result, "raw") else str(design_result)

            approval = console.input("\n[bold]✅  Aprovas o plano? (s/n):[/bold] ").strip().lower()
            if approval != "s":
                console.print("[yellow]⛔  Implementação cancelada.[/yellow]")
                status = "⛔"
                raise KeyboardInterrupt

            console.print("\n[blue]💻  FASE 2/3 — Developer[/blue]")
            impl_inputs = {**base_inputs, "architect_plan": architect_plan}
            DevStudioCrew(project_path=project_path).implement_crew().kickoff(inputs=impl_inputs)

            console.print("\n[cyan]🔍  FASE 3/3 — Reviewer[/cyan]")
            review_result = DevStudioCrew(project_path=project_path).review_crew().kickoff(inputs=impl_inputs)
            _offer_fix_cycle(review_result, impl_inputs)

            status = "✅"
        except KeyboardInterrupt:
            console.print("\n[yellow]⚠️   Pedido interrompido.[/yellow]")
            status = "⛔"
        except Exception as e:
            console.print(f"\n[red]❌  Erro:[/red] {e}")
            status = "❌"

        elapsed = time.time() - start
        session_requests.append({
            "num": request_num,
            "request": feature_request,
            "elapsed": elapsed,
            "status": status,
        })
        console.print(f"\n[green]{status}  Concluído em {elapsed:.0f}s[/green]")

    # ── End of session ──
    total = time.time() - session_start
    _print_summary(session_requests, total)
    _commit_session(project_path, branch, session_requests)

    console.print(Panel(
        f"[bold]Sessão terminada[/bold]\n"
        f"{len(session_requests)} pedidos | {total:.0f}s total",
        border_style="dim"
    ))


# ── Other entry points ───────────────────────────────────────────────────────

def train():
    inputs = {
        "feature_request": "Add a new endpoint /api/health",
        "project_path": os.environ.get("TARGET_PROJECT", ""),
        "review_feedback": "",
    }
    DevStudioCrew(project_path=inputs["project_path"]).crew().train(
        n_iterations=int(sys.argv[1]), filename=sys.argv[2], inputs=inputs
    )


def replay():
    DevStudioCrew().crew().replay(task_id=sys.argv[1])


def test():
    inputs = {
        "feature_request": "Add a new endpoint /api/health",
        "project_path": os.environ.get("TARGET_PROJECT", ""),
        "review_feedback": "",
    }
    DevStudioCrew(project_path=inputs["project_path"]).crew().test(
        n_iterations=int(sys.argv[1]), openai_model_name=sys.argv[2], inputs=inputs
    )


if __name__ == "__main__":
    run()
