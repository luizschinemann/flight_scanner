"""
FlightScanner — ponto de entrada CLI.

Comandos disponíveis:
  setup     Assistente interativo para criar/editar o config.yaml
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
    output: str = typer.Option("config.yaml", "--output", "-o", help="Arquivo de saída"),
):
    """Assistente interativo para criar ou editar o config.yaml."""
    import yaml
    from rich.prompt import Confirm, IntPrompt, Prompt
    from rich.panel import Panel

    console.print(
        Panel(
            "[bold cyan]FlightScanner[/bold cyan] — Assistente de Configuração\n"
            "[dim]Responda as perguntas para gerar seu config.yaml[/dim]",
            expand=False,
        )
    )

    # ── Config existente ──────────────────────────────────────────────────────
    config_path = Path(output)
    if config_path.exists():
        console.print(f"\n[yellow]⚠  Arquivo[/yellow] [bold]{output}[/bold] [yellow]já existe.[/yellow]")
        if not Confirm.ask("  Deseja sobrescrevê-lo?", default=False):
            raise typer.Exit()

    # ── Buscas ────────────────────────────────────────────────────────────────
    console.rule("\n[bold blue]Buscas de Voo[/bold blue]")
    _MES_ABBR = ["", "jan", "fev", "mar", "abr", "mai", "jun",
                 "jul", "ago", "set", "out", "nov", "dez"]
    searches = []
    idx = 1
    while True:
        console.print(f"\n  [bold]Busca #{idx}[/bold]")
        origin      = Prompt.ask("    Origem  (código IATA, ex: VDC)").upper().strip()
        destination = Prompt.ask("    Destino (código IATA, ex: GRU)").upper().strip()
        outbound    = Prompt.ask("    Data de ida      (YYYY-MM-DD)")
        round_trip  = Confirm.ask("    Ida e volta?", default=True)
        return_date = None
        if round_trip:
            return_date = Prompt.ask("    Data de volta    (YYYY-MM-DD)")
        adults = IntPrompt.ask("    Adultos", default=1)
        cabin  = Prompt.ask(
            "    Classe",
            choices=["economy", "business", "first"],
            default="economy",
        )

        # ID e descrição automáticos
        try:
            m  = int(outbound[5:7])
            yy = outbound[2:4]
            auto_id = f"{origin.lower()}-{destination.lower()}-{_MES_ABBR[m]}{yy}"
            volta_str = f"→{return_date[8:10]}" if return_date else ""
            auto_desc = (
                f"{origin} ↔ {destination} — "
                f"{outbound[8:10]}{volta_str}/{outbound[5:7]}/{outbound[:4]}"
            )
        except Exception:
            auto_id   = f"busca-{idx}"
            auto_desc = f"{origin} → {destination} {outbound}"

        description = Prompt.ask("    Descrição", default=auto_desc)
        search_id   = Prompt.ask("    ID único",  default=auto_id)

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
        console.print(f"  [green]✓[/green] [bold]{description}[/bold] adicionada.")
        idx += 1

        if not Confirm.ask("\n  Adicionar outra busca?", default=False):
            break

    # ── Agendamento ───────────────────────────────────────────────────────────
    console.rule("\n[bold blue]Agendamento[/bold blue]")
    use_times = Confirm.ask(
        "\n  Usar horários fixos do dia? (ex: 08:00 e 20:00)", default=False
    )
    interval_minutes = 60
    run_at_times: list[str] = []
    if use_times:
        raw = Prompt.ask("  Horários separados por vírgula", default="08:00,20:00")
        run_at_times = [t.strip() for t in raw.split(",") if t.strip()]
    else:
        interval_minutes = IntPrompt.ask("  Intervalo entre rodadas (minutos)", default=60)
    start_immediately = Confirm.ask("  Executar imediatamente ao iniciar?", default=True)

    # ── Telegram ──────────────────────────────────────────────────────────────
    console.rule("\n[bold blue]Notificações — Telegram[/bold blue]")
    tg_enabled = Confirm.ask("\n  Habilitar notificações pelo Telegram?", default=True)
    bot_token = ""
    chat_id   = ""
    if tg_enabled:
        console.print(
            "  [dim]Token → @BotFather no Telegram  |  Chat ID → @userinfobot[/dim]"
        )
        bot_token = Prompt.ask("  Bot Token")
        chat_id   = Prompt.ask("  Chat ID")

    threshold_str = Prompt.ask(
        "  % mínima de queda para alerta especial [dim](0 = alerta sempre)[/dim]",
        default="0.0",
    )

    # ── Scraper ───────────────────────────────────────────────────────────────
    console.rule("\n[bold blue]Scraper[/bold blue]")
    headless = Confirm.ask(
        "\n  Executar o Chrome em modo headless (sem janela)?", default=True
    )

    # ── Gera e escreve YAML ───────────────────────────────────────────────────
    config_data = {
        "app": {"currency": "BRL", "language": "pt-BR"},
        "schedule": {
            "interval_minutes": interval_minutes,
            "start_immediately": start_immediately,
            "run_at_times":      run_at_times,
        },
        "searches": searches,
        "notifications": {
            "console":                  True,
            "price_drop_threshold_pct": float(threshold_str),
            "webhook_url":              "",
            "telegram": {
                "enabled":   tg_enabled,
                "bot_token": bot_token,
                "chat_id":   chat_id,
            },
        },
        "storage": {
            "db_path":          "./data/flights.db",
            "keep_history_days": 90,
        },
        "scraper": {
            "headless":                   headless,
            "timeout_ms":                 35000,
            "screenshot_on_error":        True,
            "retry_attempts":             2,
            "delay_between_searches_sec": 8,
            "proxy":                      "",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    }

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        yaml.dump(
            config_data, f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )

    console.print()
    console.print(
        Panel(
            f"[bold green]✓  {output} criado com sucesso![/bold green]\n\n"
            f"  {len(searches)} busca(s) configurada(s)\n\n"
            f"  [bold]Próximos passos:[/bold]\n"
            f"  [cyan]python main.py install[/cyan]   → instalar o Chromium\n"
            f"  [cyan]python main.py search[/cyan]    → testar uma busca agora\n"
            f"  [cyan]python main.py run[/cyan]        → iniciar o monitor contínuo",
            title="[bold green]Configuração concluída[/bold green]",
            expand=False,
        )
    )


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
