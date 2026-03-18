"""
Armazenamento de histórico de preços em SQLite.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ── Modelo de resultado ─────────────────────────────────────────────────────────

@dataclass
class FlightResult:
    search_id: str
    origin: str
    destination: str
    outbound_date: str
    return_date: Optional[str]
    price: float
    currency: str
    airline: str
    departure_time: str
    arrival_time: str
    duration: str
    stops: int
    scraped_at: str = ""

    def __post_init__(self):
        if not self.scraped_at:
            self.scraped_at = datetime.now().isoformat(timespec="seconds")


@dataclass
class PriceRecord:
    """Registro completo (inclui id do banco)."""
    id: int
    search_id: str
    origin: str
    destination: str
    outbound_date: str
    return_date: Optional[str]
    price: float
    currency: str
    airline: str
    departure_time: str
    arrival_time: str
    duration: str
    stops: int
    scraped_at: str


# ── Banco de dados ─────────────────────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS flight_prices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id       TEXT    NOT NULL,
    origin          TEXT    NOT NULL,
    destination     TEXT    NOT NULL,
    outbound_date   TEXT    NOT NULL,
    return_date     TEXT,
    price           REAL    NOT NULL,
    currency        TEXT    NOT NULL DEFAULT 'BRL',
    airline         TEXT    NOT NULL DEFAULT '',
    departure_time  TEXT    NOT NULL DEFAULT '',
    arrival_time    TEXT    NOT NULL DEFAULT '',
    duration        TEXT    NOT NULL DEFAULT '',
    stops           INTEGER NOT NULL DEFAULT -1,
    scraped_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_search_date
    ON flight_prices (search_id, scraped_at);
"""


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(DDL)

    # ── Escrita ──────────────────────────────────────────────────────────────

    def save_results(self, results: list[FlightResult]) -> int:
        """Persiste uma lista de resultados e retorna quantos foram inseridos."""
        if not results:
            return 0
        rows = [
            (
                r.search_id, r.origin, r.destination, r.outbound_date,
                r.return_date, r.price, r.currency, r.airline,
                r.departure_time, r.arrival_time, r.duration, r.stops,
                r.scraped_at,
            )
            for r in results
        ]
        with self._conn() as conn:
            conn.executemany(
                """INSERT INTO flight_prices
                   (search_id, origin, destination, outbound_date, return_date,
                    price, currency, airline, departure_time, arrival_time,
                    duration, stops, scraped_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        return len(rows)

    def purge_old_records(self, keep_days: int):
        """Remove registros mais antigos que `keep_days` dias."""
        cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM flight_prices WHERE scraped_at < ?", (cutoff,)
            )

    # ── Leitura ──────────────────────────────────────────────────────────────

    def get_min_price(self, search_id: str) -> Optional[float]:
        """Menor preço já registrado para uma busca."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MIN(price) FROM flight_prices WHERE search_id = ?",
                (search_id,),
            ).fetchone()
        return row[0] if row and row[0] is not None else None

    def get_last_run_price(self, search_id: str) -> Optional[float]:
        """Preço da rodada imediatamente anterior à última."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT DISTINCT scraped_at FROM flight_prices
                   WHERE search_id = ?
                   ORDER BY scraped_at DESC LIMIT 2""",
                (search_id,),
            ).fetchall()
        if len(rows) < 2:
            return None
        prev_ts = rows[1][0]
        with self._conn() as conn:
            row = conn.execute(
                """SELECT MIN(price) FROM flight_prices
                   WHERE search_id = ? AND scraped_at = ?""",
                (search_id, prev_ts),
            ).fetchone()
        return row[0] if row and row[0] is not None else None

    def get_history(self, search_id: str, limit: int = 50) -> list[PriceRecord]:
        """Histórico dos `limit` registros mais recentes para uma busca."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM flight_prices
                   WHERE search_id = ?
                   ORDER BY scraped_at DESC
                   LIMIT ?""",
                (search_id, limit),
            ).fetchall()
        return [PriceRecord(**dict(r)) for r in rows]

    def get_cheapest_per_run(self, search_id: str, last_n: int = 20) -> list[dict]:
        """Retorna o menor preço de cada rodada (para gráfico de tendência)."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT scraped_at, MIN(price) as price, currency
                   FROM flight_prices
                   WHERE search_id = ?
                   GROUP BY scraped_at
                   ORDER BY scraped_at DESC
                   LIMIT ?""",
                (search_id, last_n),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_all_search_ids(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT search_id FROM flight_prices"
            ).fetchall()
        return [r[0] for r in rows]
