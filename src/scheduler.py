"""
Orquestrador: executa as buscas e agenda as rodadas.

Suporta dois modos de agendamento (configurável em config.yaml):
  • interval_minutes  — intervalo fixo entre rodadas (ex: 60 min)
  • run_at_times      — lista de horários do dia (ex: ["08:00", "14:00", "20:00"])
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from .config import AppConfig
from .notifier import Notifier, console
from .scraper import FlightScraper
from .storage import Storage


class FlightScheduler:
    def __init__(self, config: AppConfig):
        self.config    = config
        self.storage   = Storage(config.storage.db_path)
        self.notifier  = Notifier(config.notifications)
        self.scraper   = FlightScraper(
            config.scraper, config.currency, config.language
        )
        self._run_count = 0
        self._scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")
        self._bot = None
        self._bot_task = None

    # ── Execução de uma rodada ─────────────────────────────────────────────────

    async def run_once(self):
        """Executa todas as buscas configuradas em sequência."""
        self._run_count += 1
        self.notifier.notify_run_start(self._run_count)

        any_result = False
        delay = self.config.scraper.delay_between_searches_sec

        for i, search in enumerate(self.config.searches):
            logger.info(f"Buscando: {search.label}")

            try:
                results = await self.scraper.search(search)
            except Exception as exc:
                logger.error(f"Erro na busca [{search.id}]: {exc}")
                results = []

            # Recupera preços históricos para comparação
            prev_min  = self.storage.get_last_run_price(search.id)
            all_time  = self.storage.get_min_price(search.id)

            # Persiste novos resultados
            saved = self.storage.save_results(results)
            logger.debug(f"[{search.id}] {saved} registros salvos")

            # Notifica
            self.notifier.notify_results(search, results, prev_min, all_time)

            if results:
                any_result = True

            # Pausa entre buscas (exceto após a última)
            if i < len(self.config.searches) - 1 and delay > 0:
                logger.debug(f"Aguardando {delay}s antes da próxima busca…")
                await asyncio.sleep(delay)

        if not any_result:
            from .notifier import print_no_results_warning
            print_no_results_warning()

        # Limpa registros antigos uma vez por rodada
        self.storage.purge_old_records(self.config.storage.keep_history_days)
        logger.debug("Limpeza do histórico concluída")

    # ── Agendamento ────────────────────────────────────────────────────────────

    async def start(self):
        """Inicia o browser, configura o scheduler e bloqueia até Ctrl+C."""
        logger.info("Iniciando browser…")
        await self.scraper.start()

        sched = self.config.schedule

        if sched.run_at_times:
            # Horários fixos: ex. ["08:00", "12:00", "20:00"]
            for time_str in sched.run_at_times:
                try:
                    hour, minute = [int(x) for x in time_str.split(":")]
                    trigger = CronTrigger(hour=hour, minute=minute)
                    self._scheduler.add_job(self._job, trigger=trigger)
                    logger.info(f"Agendado para rodar às {time_str}")
                except ValueError:
                    logger.error(f"Horário inválido em run_at_times: '{time_str}'")
        else:
            # Intervalo fixo
            minutes = sched.interval_minutes
            trigger = IntervalTrigger(minutes=minutes)
            self._scheduler.add_job(self._job, trigger=trigger)
            logger.info(f"Agendado para rodar a cada {minutes} minuto(s)")

        self._scheduler.start()

        # ── Telegram Bot (polling em background) ─────────────────────────────
        tg = self.config.notifications.telegram
        if tg.enabled and tg.bot_token and tg.chat_id:
            from .telegram_bot import TelegramBot
            self._bot = TelegramBot(tg.bot_token, tg.chat_id, self.storage, self)
            self._bot_task = asyncio.create_task(self._bot.start())
            logger.info("Telegram Bot ativo — comandos: /status /buscar /historico")

        if sched.start_immediately:
            logger.info("Executando primeira rodada imediatamente…")
            await self.run_once()

        console.print(
            f"\n[dim]Pressione[/dim] [bold]Ctrl+C[/bold] [dim]para encerrar.[/dim]\n"
        )

        try:
            # Mantém o event-loop vivo
            while True:
                await asyncio.sleep(60)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self._shutdown()

    async def _job(self):
        """Callback do APScheduler (já está no loop asyncio)."""
        try:
            await self.run_once()
        except Exception as exc:
            logger.error(f"Erro não tratado na rodada: {exc}")

    async def _shutdown(self):
        logger.info("Encerrando…")
        if self._bot:
            self._bot.stop()
        if self._bot_task:
            self._bot_task.cancel()
        self._scheduler.shutdown(wait=False)
        await self.scraper.stop()
        logger.info("Encerrado.")
