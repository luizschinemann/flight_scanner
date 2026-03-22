"""
Notificações: console (Rich) + Discord/Slack webhook + Telegram.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import httpx
from loguru import logger
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import NotificationConfig, SearchConfig
from .storage import FlightResult


console = Console()


# ── Formatadores ───────────────────────────────────────────────────────────────

def _fmt_price(price: float, currency: str = "BRL") -> str:
    if currency == "BRL":
        return f"R$ {price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{currency} {price:,.2f}"


def _fmt_stops(stops: int) -> str:
    if stops == 0:
        return "[green]Direto[/green]"
    if stops == 1:
        return "[yellow]1 escala[/yellow]"
    if stops < 0:
        return "[dim]–[/dim]"
    return f"[red]{stops} escalas[/red]"


def _price_trend(current: float, previous: Optional[float]) -> str:
    if previous is None:
        return "[dim]novo[/dim]"
    diff = current - previous
    pct = (diff / previous) * 100 if previous else 0
    if diff < 0:
        return f"[bold green]▼ {abs(pct):.1f}%[/bold green]"
    if diff > 0:
        return f"[bold red]▲ {pct:.1f}%[/bold red]"
    return "[dim]=[/dim]"


# ── Console ────────────────────────────────────────────────────────────────────

def print_run_header(run_number: int):
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    console.rule(
        f"[bold cyan]FlightScanner[/bold cyan]  "
        f"[dim]rodada #{run_number}  •  {now}[/dim]",
        style="cyan",
    )


def print_results_table(
    search: SearchConfig,
    results: list[FlightResult],
    prev_min_price: Optional[float],
    all_time_min: Optional[float],
):
    if not results:
        console.print(
            Panel(
                f"[yellow]Nenhum resultado para[/yellow] [bold]{search.label}[/bold]",
                expand=False,
            )
        )
        return

    cheapest = results[0]

    # Verifica se há voos com horário de volta para ajustar colunas
    has_return = any(r.return_departure_time for r in results[:10])

    table = Table(
        title=f"[bold]{search.label}[/bold]",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold blue",
        expand=False,
        min_width=80,
    )
    table.add_column("#",          style="dim", width=3, justify="right")
    table.add_column("Preço",      style="bold", justify="right", min_width=14)
    table.add_column("Variação",   justify="center", min_width=10)
    table.add_column("Companhia",  min_width=20)
    table.add_column("Ida (Saída)",      justify="center", min_width=7)
    table.add_column("Ida (Chegada)",    justify="center", min_width=7)
    if has_return:
        table.add_column("Volta (Saída)",  justify="center", min_width=7)
        table.add_column("Volta (Chegada)", justify="center", min_width=7)
    table.add_column("Duração",    justify="center", min_width=7)
    table.add_column("Escalas",    justify="center", min_width=10)

    for i, r in enumerate(results[:10], 1):
        # Variação em relação ao mínimo anterior
        trend = _price_trend(r.price, prev_min_price) if i == 1 else ""
        price_style = "bold green" if i == 1 else ""

        row_data = [
            str(i),
            Text(_fmt_price(r.price, r.currency), style=price_style),
            Text.from_markup(trend),
            r.airline or "–",
            r.departure_time or "–",
            r.arrival_time or "–",
        ]

        if has_return:
            row_data.extend([
                r.return_departure_time or "–",
                r.return_arrival_time or "–",
            ])

        row_data.extend([
            r.duration or "–",
            Text.from_markup(_fmt_stops(r.stops)),
        ])

        table.add_row(*row_data)

    console.print(table)

    # Linha de resumo
    hl_parts = [f"Mais barato agora: [bold green]{_fmt_price(cheapest.price, cheapest.currency)}[/bold green]"]
    if all_time_min is not None:
        hl_parts.append(f"  Mínimo histórico: [bold]{_fmt_price(all_time_min, cheapest.currency)}[/bold]")
    console.print("  " + "  •  ".join(hl_parts))
    console.print()


def print_alert(search: SearchConfig, current: float, previous: float, currency: str):
    pct = abs((current - previous) / previous * 100)
    console.print(
        Panel(
            f"[bold green]🔔  QUEDA DE PREÇO  ▼{pct:.1f}%[/bold green]\n"
            f"  [bold]{search.label}[/bold]\n"
            f"  Antes: {_fmt_price(previous, currency)}  →  "
            f"Agora: [bold green]{_fmt_price(current, currency)}[/bold green]",
            title="[bold green]ALERTA DE PREÇO[/bold green]",
            border_style="green",
            expand=False,
        )
    )


def print_no_results_warning():
    console.print("[yellow]Rodada concluída sem resultados.[/yellow]\n")


# ── Notificador principal ──────────────────────────────────────────────────────

class Notifier:
    def __init__(self, config: NotificationConfig):
        self.config = config

    def notify_results(
        self,
        search: SearchConfig,
        results: list[FlightResult],
        prev_min_price: Optional[float],
        all_time_min: Optional[float],
    ):
        if self.config.console:
            print_results_table(search, results, prev_min_price, all_time_min)

        if not results:
            return

        cheapest = results[0]

        # Envia resumo sempre que houver resultados
        self._send_telegram_summary(search, results, prev_min_price, all_time_min)

        # Verifica se houve queda de preço relevante
        if prev_min_price is not None and cheapest.price < prev_min_price:
            drop_pct = (prev_min_price - cheapest.price) / prev_min_price * 100
            if drop_pct >= self.config.price_drop_threshold_pct:
                if self.config.console:
                    print_alert(search, cheapest.price, prev_min_price, cheapest.currency)
                self._send_webhook_alert(search, cheapest, prev_min_price, drop_pct)

    def notify_run_start(self, run_number: int):
        if self.config.console:
            print_run_header(run_number)

    # ── Webhook (Discord / Slack) ──────────────────────────────────────────────

    def _send_webhook_alert(
        self,
        search: SearchConfig,
        result: FlightResult,
        prev_price: float,
        drop_pct: float,
    ):
        if not self.config.webhook_url:
            return
        # Monta mensagem com horários de ida e volta
        horarios = f"Ida: {result.departure_time or '–'} → {result.arrival_time or '–'}"
        if result.return_departure_time and result.return_arrival_time:
            horarios += f"\nVolta: {result.return_departure_time} → {result.return_arrival_time}"

        message = (
            f"🔔 **QUEDA DE PREÇO ▼{drop_pct:.1f}%**\n"
            f"**{search.label}**\n"
            f"Antes: {_fmt_price(prev_price, result.currency)}  →  "
            f"Agora: **{_fmt_price(result.price, result.currency)}**\n"
            f"Companhia: {result.airline or '–'}\n"
            f"{horarios}\n"
            f"Escalas: {result.stops if result.stops >= 0 else '–'}"
        )

        # Tenta Discord (payload com "content") e Slack (payload com "text")
        payloads = [
            {"content": message},           # Discord
            {"text": message},              # Slack
        ]
        for payload in payloads:
            try:
                resp = httpx.post(
                    self.config.webhook_url, json=payload, timeout=10
                )
                if resp.status_code < 300:
                    logger.info("Webhook enviado com sucesso")
                    return
            except Exception as exc:
                logger.warning(f"Falha ao enviar webhook: {exc}")

    # ── Telegram ───────────────────────────────────────────────────────────────

    def _send_telegram_summary(
        self,
        search: SearchConfig,
        results: list[FlightResult],
        prev_min: Optional[float],
        all_time_min: Optional[float],
    ):
        tg = self.config.telegram
        if not tg.enabled or not tg.bot_token or not tg.chat_id:
            return

        # ── Deduplica por preço (round-trip gera entradas duplicadas no DOM) ──
        seen: dict[int, FlightResult] = {}
        for r in results:
            key = round(r.price)
            if key not in seen:
                seen[key] = r
            else:
                existing = seen[key]
                if r.airline and r.departure_time and not (existing.airline and existing.departure_time):
                    seen[key] = r
        deduped = sorted(seen.values(), key=lambda r: r.price)

        # Filtra entradas sem CIA nem horário; fallback para lista completa
        good = [r for r in deduped if r.airline and r.departure_time]
        if not good:
            good = deduped

        cheapest = good[0]
        now = datetime.now().strftime("%d/%m às %H:%M")

        # ── Rota ─────────────────────────────────────────────────────────
        if search.description:
            route = search.description.split(" — ")[0].replace("↔", "→").strip()
        else:
            route = f"{search.origin} → {search.destination}"

        # ── Datas legíveis ───────────────────────────────────────────────
        _MES = ["","Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
        def _fmt_d(d: Optional[str]) -> str:
            if not d:
                return ""
            _, m, day = d.split("-")
            return f"{int(day):02d}/{_MES[int(m)]}"

        datas = f"Ida {_fmt_d(search.outbound_date)}"
        if search.return_date:
            datas += f"  ·  Volta {_fmt_d(search.return_date)}"

        # ── Variação ─────────────────────────────────────────────────────
        if prev_min is None:
            trend_line = "🆕 Primeira consulta"
        elif cheapest.price < prev_min:
            pct = (prev_min - cheapest.price) / prev_min * 100
            trend_line = f"🟢 Caiu {pct:.1f}% (era {_fmt_price(prev_min, cheapest.currency)})"
        elif cheapest.price > prev_min:
            pct = (cheapest.price - prev_min) / prev_min * 100
            trend_line = f"🔴 Subiu {pct:.1f}% (era {_fmt_price(prev_min, cheapest.currency)})"
        else:
            trend_line = "⚪ Sem alteração"

        # ── Helpers de formatação ─────────────────────────────────────────
        def _fmt_dur(dur: str) -> str:
            """'2h5min' → '2h 5min', '2h' → '2h'"""
            if not dur or "h" not in dur:
                return dur or ""
            parts = dur.split("h")
            return f"{parts[0]}h {parts[1]}" if len(parts) == 2 and parts[1] else dur

        def _fmt_stops_plain(stops: int) -> str:
            if stops == 0:
                return "Direto"
            if stops == 1:
                return "1 escala"
            if stops > 1:
                return f"{stops} escalas"
            return ""

        # ── Linha do melhor voo (3-4 linhas) ─────────────────────────────
        def _best_row(r: FlightResult) -> str:
            line1 = _fmt_price(r.price, r.currency)
            if r.airline:
                line1 += f"  ·  {r.airline}"

            # Linha 2: Ida
            time_parts_out = []
            if r.departure_time and r.arrival_time:
                time_parts_out.append(f"Ida: {r.departure_time} → {r.arrival_time}")
            if r.duration:
                time_parts_out.append(_fmt_dur(r.duration))
            s = _fmt_stops_plain(r.stops)
            if s:
                time_parts_out.append(s)
            line2 = "  ·  ".join(time_parts_out)

            # Linha 3: Volta (se houver)
            line3 = ""
            if r.return_departure_time and r.return_arrival_time:
                line3 = f"Volta: {r.return_departure_time} → {r.return_arrival_time}"

            result = [line1]
            if line2:
                result.append(line2)
            if line3:
                result.append(line3)
            return "\n".join(result)

        # ── Linha compacta (outras opções) ────────────────────────────────
        def _compact_row(r: FlightResult) -> str:
            parts = [_fmt_price(r.price, r.currency)]
            if r.airline:
                parts.append(r.airline)
            # Ida
            if r.departure_time and r.arrival_time:
                parts.append(f"Ida: {r.departure_time} → {r.arrival_time}")
            # Volta (se houver)
            if r.return_departure_time and r.return_arrival_time:
                parts.append(f"Volta: {r.return_departure_time} → {r.return_arrival_time}")
            # Escalas
            s = _fmt_stops_plain(r.stops)
            if s:
                parts.append(s)
            return "  ·  ".join(parts)

        # ── Monta a mensagem ──────────────────────────────────────────────
        lines = [
            f"✈️ {route}",
            f"📅 {datas}",
            f"🕐 {now}",
            "",
            "",
            "— MELHOR OPÇÃO —",
            "",
            _best_row(cheapest),
            trend_line,
        ]

        outros = good[1:4]
        if outros:
            lines += [
                "",
                "",
                "— OUTRAS OPÇÕES —",
                "",
            ]
            for r in outros:
                lines.append(_compact_row(r))

        if all_time_min is not None:
            lines += [
                "",
                "",
                f"📊 Mínimo histórico: {_fmt_price(all_time_min, cheapest.currency)}",
            ]

        text = "\n".join(lines)
        url = f"https://api.telegram.org/bot{tg.bot_token}/sendMessage"
        try:
            httpx.post(
                url,
                json={"chat_id": tg.chat_id, "text": text},
                timeout=10,
            )
            logger.info(f"[{search.id}] Resumo Telegram enviado")
        except Exception as exc:
            logger.warning(f"Falha ao enviar resumo Telegram: {exc}")

    def _send_telegram_alert(
        self,
        search: SearchConfig,
        result: FlightResult,
        prev_price: float,
        drop_pct: float,
    ):
        tg = self.config.telegram
        if not tg.enabled or not tg.bot_token or not tg.chat_id:
            return
        text = (
            f"🔔 *QUEDA DE PREÇO ▼{drop_pct:.1f}%*\n"
            f"*{search.label}*\n"
            f"Antes: {_fmt_price(prev_price, result.currency)}\n"
            f"Agora: *{_fmt_price(result.price, result.currency)}*\n"
            f"Companhia: {result.airline or '–'}\n"
            f"Saída: {result.departure_time or '–'}  Chegada: {result.arrival_time or '–'}"
        )
        url = f"https://api.telegram.org/bot{tg.bot_token}/sendMessage"
        try:
            httpx.post(
                url,
                json={"chat_id": tg.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
            logger.info("Notificação Telegram enviada")
        except Exception as exc:
            logger.warning(f"Falha ao enviar Telegram: {exc}")
