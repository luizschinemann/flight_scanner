"""
FlightScanner — ponto de entrada CLI.

Comandos disponíveis:
  setup     Gerencia as buscas de voo (adicionar / remover)
  run       Inicia o agendador contínuo (loop principal)
  search    Executa uma rodada avulsa (uma vez) e sai
  history   Exibe histórico de preços do banco
  install   Instala o browser Chromium via Playwright
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

# ── UTF-8 no terminal Windows ──────────────────────────────────────────────────
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table
from rich import box

# ── Fix Windows asyncio — Playwright precisa do ProactorEventLoop ──────────────
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# ── App ────────────────────────────────────────────────────────────────────────
app     = typer.Typer(help="FlightScanner — monitor automático de passagens aéreas")
console = Console()

_DEFAULT_CONFIG = "config.yaml"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool):
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        colorize=True,
    )
    logger.add(
        "data/flightscanner.log",
        level="DEBUG",
        rotation="10 MB",
        retention=5,
        encoding="utf-8",
    )


def _load(config_path: str):
    from src.config import load_config
    try:
        return load_config(config_path)
    except FileNotFoundError as e:
        console.print(f"[bold red]Erro:[/bold red] {e}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[bold red]Config inválida:[/bold red] {e}")
        raise typer.Exit(1)


# ── Comandos ───────────────────────────────────────────────────────────────────

@app.command()
def run(
    config: str = typer.Option(_DEFAULT_CONFIG, "--config", "-c", help="Caminho para config.yaml"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Modo verboso (DEBUG)"),
):
    """Inicia o monitor contínuo com agendamento configurado."""
    _setup_logging(verbose)
    cfg = _load(config)

    from src.scheduler import FlightScheduler
    scheduler = FlightScheduler(cfg)

    console.print(
        "[bold cyan]FlightScanner[/bold cyan] iniciando…\n"
        f"  [dim]{len(cfg.searches)} busca(s) configurada(s)[/dim]"
    )

    asyncio.run(scheduler.start())


@app.command()
def search(
    config: str = typer.Option(_DEFAULT_CONFIG, "--config", "-c", help="Caminho para config.yaml"),
    search_id: Optional[str] = typer.Option(None, "--id", "-i", help="ID da busca (deixe vazio para todas)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Modo verboso (DEBUG)"),
):
    """Executa uma rodada de buscas imediata (sem agendamento) e exibe resultados."""
    _setup_logging(verbose)
    cfg = _load(config)

    targets = cfg.searches
    if search_id:
        targets = [s for s in cfg.searches if s.id == search_id]
        if not targets:
            console.print(f"[bold red]ID '[/bold red]{search_id}[bold red]' não encontrado.[/bold red]")
            raise typer.Exit(1)

    # Executa rodada única
    from src.scheduler import FlightScheduler
    scheduler = FlightScheduler(cfg)
    # Substitui lista para processar só os targets selecionados
    scheduler.config.searches = targets

    async def _once():
        await scheduler.scraper.start()
        try:
            await scheduler.run_once()
        finally:
            await scheduler.scraper.stop()

    asyncio.run(_once())


@app.command()
def history(
    config: str = typer.Option(_DEFAULT_CONFIG, "--config", "-c", help="Caminho para config.yaml"),
    search_id: Optional[str] = typer.Option(None, "--id", "-i", help="ID da busca"),
    limit: int = typer.Option(20, "--limit", "-n", help="Quantidade de registros por busca"),
):
    """Exibe histórico de preços do banco de dados."""
    cfg = _load(config)

    from src.storage import Storage
    storage = Storage(cfg.storage.db_path)

    ids = [search_id] if search_id else storage.get_all_search_ids()
    if not ids:
        console.print("[yellow]Nenhum dado no banco ainda.[/yellow]")
        raise typer.Exit()

    for sid in ids:
        records = storage.get_history(sid, limit=limit)
        if not records:
            continue

        t = Table(
            title=f"Histórico: [bold]{sid}[/bold]",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold blue",
        )
        t.add_column("Data/Hora",   style="dim",   min_width=19)
        t.add_column("Preço",       justify="right", min_width=14)
        t.add_column("Companhia",   min_width=18)
        t.add_column("Saída",       justify="center", min_width=7)
        t.add_column("Chegada",     justify="center", min_width=7)
        t.add_column("Escalas",     justify="center", min_width=8)

        for r in records:
            stops_str = (
                "Direto" if r.stops == 0
                else f"{r.stops} escala(s)" if r.stops > 0
                else "–"
            )
            price_str = f"R$ {r.price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            t.add_row(
                r.scraped_at[:19],
                price_str,
                r.airline or "–",
                r.departure_time or "–",
                r.arrival_time or "–",
                stops_str,
            )

        console.print(t)

        # Tendência (mínimo por rodada)
        trend = storage.get_cheapest_per_run(sid, last_n=10)
        if len(trend) >= 2:
            prices = [row["price"] for row in trend]
            delta  = prices[-1] - prices[0]
            sign   = "▼" if delta < 0 else "▲" if delta > 0 else "="
            color  = "green" if delta < 0 else "red" if delta > 0 else "dim"
            console.print(
                f"  Tendência (últimas {len(trend)} rodadas): "
                f"[{color}]{sign} R$ {abs(delta):,.2f}[/{color}]\n"
            )


@app.command()
def setup(
    config: str = typer.Option(_DEFAULT_CONFIG, "--config", "-c", help="Caminho para config.yaml"),
):
    """Gerencia as buscas de voo: adicionar ou remover sem tocar no resto do config."""
    import yaml
    from rich.prompt import Confirm, IntPrompt, Prompt
    from rich.panel import Panel

    _MES_ABBR = ["", "jan", "fev", "mar", "abr", "mai", "jun",
                 "jul", "ago", "set", "out", "nov", "dez"]

    config_path = Path(config)
    if not config_path.exists():
        console.print(
            f"[bold red]Erro:[/bold red] {config} não encontrado.\n"
            "  Copie [bold]config.yaml.example[/bold] para [bold]config.yaml[/bold] primeiro."
        )
        raise typer.Exit(1)

    # ── Carrega raw YAML para preservar todas as outras seções ──────────────
    with open(config_path, encoding="utf-8") as f:
        raw: dict = yaml.safe_load(f) or {}

    def _show_table(searches: list[dict]) -> None:
        if not searches:
            console.print("  [dim]Nenhuma busca configurada.[/dim]\n")
            return
        t = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold blue")
        t.add_column("#",      style="dim", width=3, justify="right")
        t.add_column("ID",     min_width=20)
        t.add_column("Rota",   min_width=10)
        t.add_column("Ida",    justify="center", min_width=12)
        t.add_column("Volta",  justify="center", min_width=12)
        t.add_column("Classe", justify="center", min_width=9)
        for i, s in enumerate(searches, 1):
            t.add_row(
                str(i),
                s.get("id", ""),
                f"{s.get('origin', '')} ↔ {s.get('destination', '')}",
                s.get("outbound_date", ""),
                s.get("return_date", "[dim]–[/dim]"),
                s.get("cabin", "economy"),
            )
        console.print(t)

    def _add_search(searches: list[dict]) -> None:
        console.print()
        origin      = Prompt.ask("  Origem  (IATA, ex: VDC)").upper().strip()
        destination = Prompt.ask("  Destino (IATA, ex: GRU)").upper().strip()
        outbound    = Prompt.ask("  Data de ida   (YYYY-MM-DD)")
        round_trip  = Confirm.ask("  Ida e volta?", default=True)
        return_date = None
        if round_trip:
            return_date = Prompt.ask("  Data de volta (YYYY-MM-DD)")
        adults = IntPrompt.ask("  Adultos", default=1)
        cabin  = Prompt.ask(
            "  Classe", choices=["economy", "business", "first"], default="economy"
        )

        try:
            m         = int(outbound[5:7])
            yy        = outbound[2:4]
            auto_id   = f"{origin.lower()}-{destination.lower()}-{_MES_ABBR[m]}{yy}"
            volta_str = f"→{return_date[8:10]}" if return_date else ""
            auto_desc = (
                f"{origin} ↔ {destination} — "
                f"{outbound[8:10]}{volta_str}/{outbound[5:7]}/{outbound[:4]}"
            )
        except Exception:
            auto_id   = f"busca-{len(searches) + 1}"
            auto_desc = f"{origin} → {destination} {outbound}"

        description = Prompt.ask("  Descrição", default=auto_desc)
        search_id   = Prompt.ask("  ID único",  default=auto_id)

        # Garante ID único
        existing_ids = {s["id"] for s in searches}
        while search_id in existing_ids:
            console.print(f"  [yellow]ID '{search_id}' já existe.[/yellow]")
            search_id = Prompt.ask("  ID único (diferente)")

        s: dict = {
            "id":            search_id,
            "description":   description,
            "origin":        origin,
            "destination":   destination,
            "outbound_date": outbound,
            "adults":        adults,
            "cabin":         cabin,
        }
        if return_date:
            s["return_date"] = return_date
        searches.append(s)
        console.print(f"\n  [green]✓[/green] [bold]{description}[/bold] adicionada.")

    def _remove_search(searches: list[dict]) -> None:
        if not searches:
            console.print("  [yellow]Nenhuma busca para remover.[/yellow]")
            return
        num = IntPrompt.ask(f"  Remover qual? (1–{len(searches)})")
        if 1 <= num <= len(searches):
            removed = searches.pop(num - 1)
            console.print(
                f"\n  [red]✕[/red] Busca [bold]{removed['id']}[/bold] removida."
            )
        else:
            console.print("  [yellow]Número inválido.[/yellow]")

    # ── Loop principal ────────────────────────────────────────────────────────
    searches: list[dict] = list(raw.get("searches", []))
    changed = False

    while True:
        console.print()
        console.rule("[bold cyan]FlightScanner[/bold cyan] — Buscas de Voo")
        console.print()
        _show_table(searches)

        console.print(
            "  [bold][A][/bold] Adicionar busca\n"
            "  [bold][R][/bold] Remover busca\n"
            "  [bold][S][/bold] Salvar e sair\n"
            "  [bold][Q][/bold] Sair sem salvar"
        )
        console.print()
        action = Prompt.ask("  Opção", choices=["a", "r", "s", "q"], default="s").lower()

        if action == "a":
            _add_search(searches)
            changed = True
        elif action == "r":
            _remove_search(searches)
            changed = True
        elif action == "s":
            break
        elif action == "q":
            console.print("  [dim]Saindo sem salvar.[/dim]")
            raise typer.Exit()

    # ── Salva apenas a seção searches, preservando todo o resto ──────────────
    if changed:
        raw["searches"] = searches
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(raw, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

        console.print()
        console.print(
            Panel(
                f"[bold green]✓  config.yaml salvo com {len(searches)} busca(s).[/bold green]\n\n"
                f"  [cyan]python main.py search[/cyan]  → testar agora\n"
                f"  [cyan]python main.py run[/cyan]     → iniciar o monitor",
                expand=False,
            )
        )
    else:
        console.print("\n  [dim]Nenhuma alteração realizada.[/dim]")


@app.command()
def install():
    """Instala o browser Chromium necessário para o Playwright."""
    import subprocess
    console.print("[cyan]Instalando browser Chromium (Playwright)…[/cyan]")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=False,
    )
    if result.returncode == 0:
        console.print("[bold green]Instalação concluída![/bold green]")
    else:
        console.print("[bold red]Falha na instalação. Verifique se o playwright está instalado:[/bold red]")
        console.print("  pip install playwright")


# ── Entry-point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    Path("data").mkdir(exist_ok=True)
    Path("data/screenshots").mkdir(exist_ok=True)
    app()
