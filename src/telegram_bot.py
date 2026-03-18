"""
Telegram Bot — recebe comandos e responde via long-polling.

Comandos:
  /start      Mensagem de boas-vindas
  /status     Último preço mais barato de cada rota
  /buscar     Força uma rodada de buscas agora
  /historico  Mínimo histórico por rota
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

import httpx
from loguru import logger

from .storage import Storage

if TYPE_CHECKING:
    from .scheduler import FlightScheduler


_API = "https://api.telegram.org/bot{token}"


def _fmt_price(price: float, currency: str = "BRL") -> str:
    if currency == "BRL":
        return f"R$ {price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{currency} {price:,.2f}"


class TelegramBot:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        storage: Storage,
        scheduler: FlightScheduler,
    ):
        self.token     = bot_token
        self.chat_id   = chat_id
        self.storage   = storage
        self.scheduler = scheduler
        self._base     = _API.format(token=bot_token)
        self._offset   = 0
        self._running  = False
        self._searching = False

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self):
        """Registra os comandos no BotFather e inicia o loop de polling."""
        await self._set_commands()
        self._running = True
        logger.info("Telegram Bot: polling iniciado")
        while self._running:
            try:
                await self._poll()
            except httpx.TimeoutException:
                pass
            except Exception as exc:
                logger.warning(f"Telegram Bot poll erro: {exc}")
                await asyncio.sleep(5)

    def stop(self):
        self._running = False

    # ── Polling ────────────────────────────────────────────────────────────────

    async def _poll(self):
        async with httpx.AsyncClient(timeout=35) as client:
            resp = await client.get(
                f"{self._base}/getUpdates",
                params={"offset": self._offset, "timeout": 30},
            )
        data = resp.json()
        if not data.get("ok"):
            return

        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = (msg.get("text") or "").strip()
            from_chat = str(msg.get("chat", {}).get("id", ""))

            # Só responde no chat autorizado
            if from_chat != self.chat_id:
                continue

            if text.startswith("/start"):
                await self._cmd_start()
            elif text.startswith("/status"):
                await self._cmd_status()
            elif text.startswith("/buscar"):
                await self._cmd_buscar()
            elif text.startswith("/historico"):
                await self._cmd_historico()

    # ── Comandos ───────────────────────────────────────────────────────────────

    async def _cmd_start(self):
        searches = self.scheduler.config.searches
        lines = [
            "FlightScanner ativo!",
            f"{len(searches)} busca(s) configurada(s).",
            "",
            "Comandos:",
            "/status  — ultimo preco por rota",
            "/buscar  — forcar busca agora",
            "/historico  — minimo historico",
        ]
        await self._send("\n".join(lines))

    async def _cmd_status(self):
        searches = self.scheduler.config.searches
        if not searches:
            await self._send("Nenhuma busca configurada.")
            return

        lines = ["— STATUS ATUAL —", ""]
        for s in searches:
            # Último scraped_at
            history = self.storage.get_history(s.id, limit=1)
            if not history:
                lines.append(f"{s.label}")
                lines.append("  Sem dados ainda")
                lines.append("")
                continue

            last = history[0]
            min_price = self.storage.get_min_price(s.id)

            # Pega descrição da rota
            if s.description:
                route = s.description.split(" — ")[0].replace("↔", "→").strip()
            else:
                route = f"{s.origin} → {s.destination}"

            lines.append(f"✈️ {route}")
            lines.append(
                f"  Ultimo: {_fmt_price(last.price, last.currency)}"
                f"  ({last.scraped_at[:16].replace('T', ' ')})"
            )
            if min_price is not None:
                lines.append(f"  Minimo: {_fmt_price(min_price, last.currency)}")
            lines.append("")

        await self._send("\n".join(lines))

    async def _cmd_buscar(self):
        if self._searching:
            await self._send("Ja existe uma busca em andamento...")
            return

        self._searching = True
        await self._send("Buscando... aguarde.")

        try:
            await self.scheduler.run_once()
        except Exception as exc:
            logger.error(f"Erro na busca via Telegram: {exc}")
            await self._send(f"Erro na busca: {exc}")
        finally:
            self._searching = False

    async def _cmd_historico(self):
        searches = self.scheduler.config.searches
        if not searches:
            await self._send("Nenhuma busca configurada.")
            return

        lines = ["— HISTORICO —", ""]
        for s in searches:
            trend = self.storage.get_cheapest_per_run(s.id, last_n=5)
            min_price = self.storage.get_min_price(s.id)

            if s.description:
                route = s.description.split(" — ")[0].replace("↔", "→").strip()
            else:
                route = f"{s.origin} → {s.destination}"

            lines.append(f"✈️ {route}")

            if not trend:
                lines.append("  Sem dados")
                lines.append("")
                continue

            for row in trend:
                ts = row["scraped_at"][:16].replace("T", " ")
                lines.append(f"  {ts}  {_fmt_price(row['price'], row.get('currency', 'BRL'))}")

            if min_price is not None:
                lines.append(f"  📊 Minimo: {_fmt_price(min_price, trend[0].get('currency', 'BRL'))}")
            lines.append("")

        await self._send("\n".join(lines))

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _send(self, text: str):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{self._base}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text},
                )
        except Exception as exc:
            logger.warning(f"Telegram Bot envio falhou: {exc}")

    async def _set_commands(self):
        commands = [
            {"command": "status",    "description": "Ultimo preco por rota"},
            {"command": "buscar",    "description": "Forcar busca agora"},
            {"command": "historico", "description": "Minimo historico por rota"},
        ]
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{self._base}/setMyCommands",
                    json={"commands": commands},
                )
            logger.info("Telegram Bot: comandos registrados")
        except Exception as exc:
            logger.warning(f"Telegram Bot: falha ao registrar comandos: {exc}")
