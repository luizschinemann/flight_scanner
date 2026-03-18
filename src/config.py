"""
Carregamento e validação da configuração via config.yaml.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import yaml


# ── Modelos ────────────────────────────────────────────────────────────────────

@dataclass
class SearchConfig:
    id: str
    origin: str
    destination: str
    outbound_date: str
    description: str = ""
    return_date: Optional[str] = None
    adults: int = 1
    cabin: str = "economy"

    @property
    def is_round_trip(self) -> bool:
        return self.return_date is not None

    @property
    def label(self) -> str:
        return self.description or f"{self.origin}→{self.destination} {self.outbound_date}"


@dataclass
class ScheduleConfig:
    interval_minutes: int = 60
    start_immediately: bool = True
    run_at_times: list[str] = field(default_factory=list)


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class NotificationConfig:
    console: bool = True
    price_drop_threshold_pct: float = 0.0
    webhook_url: str = ""
    telegram: TelegramConfig = field(default_factory=TelegramConfig)


@dataclass
class StorageConfig:
    db_path: str = "./data/flights.db"
    keep_history_days: int = 90


@dataclass
class ScraperConfig:
    headless: bool = True
    timeout_ms: int = 35000
    screenshot_on_error: bool = True
    retry_attempts: int = 2
    delay_between_searches_sec: int = 8
    proxy: str = ""
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )


@dataclass
class AppConfig:
    currency: str = "BRL"
    language: str = "pt-BR"
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    searches: list[SearchConfig] = field(default_factory=list)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    scraper: ScraperConfig = field(default_factory=ScraperConfig)


# ── Loader ─────────────────────────────────────────────────────────────────────

def load_config(path: str | Path = "config.yaml") -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo de configuração não encontrado: {path}")

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    app_raw = raw.get("app", {})
    sched_raw = raw.get("schedule", {})
    notif_raw = raw.get("notifications", {})
    tg_raw = notif_raw.get("telegram", {})
    stor_raw = raw.get("storage", {})
    scr_raw = raw.get("scraper", {})

    searches: list[SearchConfig] = []
    for s in raw.get("searches", []):
        searches.append(
            SearchConfig(
                id=s["id"],
                origin=s["origin"].upper(),
                destination=s["destination"].upper(),
                outbound_date=s["outbound_date"],
                description=s.get("description", ""),
                return_date=s.get("return_date"),
                adults=int(s.get("adults", 1)),
                cabin=s.get("cabin", "economy"),
            )
        )

    return AppConfig(
        currency=app_raw.get("currency", "BRL"),
        language=app_raw.get("language", "pt-BR"),
        schedule=ScheduleConfig(
            interval_minutes=int(sched_raw.get("interval_minutes", 60)),
            start_immediately=bool(sched_raw.get("start_immediately", True)),
            run_at_times=list(sched_raw.get("run_at_times", [])),
        ),
        searches=searches,
        notifications=NotificationConfig(
            console=bool(notif_raw.get("console", True)),
            price_drop_threshold_pct=float(notif_raw.get("price_drop_threshold_pct", 5.0)),
            webhook_url=str(notif_raw.get("webhook_url", "") or ""),
            telegram=TelegramConfig(
                enabled=bool(tg_raw.get("enabled", False)),
                bot_token=str(tg_raw.get("bot_token", "") or ""),
                chat_id=str(tg_raw.get("chat_id", "") or ""),
            ),
        ),
        storage=StorageConfig(
            db_path=str(stor_raw.get("db_path", "./data/flights.db")),
            keep_history_days=int(stor_raw.get("keep_history_days", 90)),
        ),
        scraper=ScraperConfig(
            headless=bool(scr_raw.get("headless", True)),
            timeout_ms=int(scr_raw.get("timeout_ms", 35000)),
            screenshot_on_error=bool(scr_raw.get("screenshot_on_error", True)),
            retry_attempts=int(scr_raw.get("retry_attempts", 2)),
            delay_between_searches_sec=int(scr_raw.get("delay_between_searches_sec", 8)),
            proxy=str(scr_raw.get("proxy", "") or ""),
            user_agent=str(scr_raw.get("user_agent", ScraperConfig.user_agent)),
        ),
    )
