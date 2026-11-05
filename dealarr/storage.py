"""Persistance SQLite : relevés de prix, état des alertes, échecs, stats externes."""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    product_id TEXT NOT NULL,
    offer_url TEXT NOT NULL,
    store TEXT NOT NULL,
    ts INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    in_stock INTEGER,
    method TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_product_ts ON observations(product_id, ts);
CREATE INDEX IF NOT EXISTS idx_obs_offer_ts ON observations(offer_url, ts);

CREATE TABLE IF NOT EXISTS alert_state (
    product_id TEXT PRIMARY KEY,
    price_cents INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    rules TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    product_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    store TEXT,
    rules TEXT
);

CREATE TABLE IF NOT EXISTS failures (
    offer_url TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    last_error TEXT,
    last_ts INTEGER,
    notified INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS external_stats (
    product_id TEXT NOT NULL,
    source TEXT NOT NULL,
    ts INTEGER NOT NULL,
    low_cents INTEGER,
    avg_cents INTEGER,
    PRIMARY KEY (product_id, source)
);
"""


@dataclass(frozen=True)
class ExternalStats:
    source: str
    low_cents: int | None
    avg_cents: int | None
    ts: int


@dataclass(frozen=True)
class LatestObservation:
    offer_url: str
    store: str
    ts: int
    price_cents: int
    total_cents: int
    in_stock: bool | None
    shipping_src: str | None = None

    @property
    def shipping_cents(self) -> int:
        return self.total_cents - self.price_cents


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=15, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(observations)")}
        if "shipping_src" not in cols:  # migration v0.1 → v0.2
            self.conn.execute("ALTER TABLE observations ADD COLUMN shipping_src TEXT")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- relevés -------------------------------------------------------------------------

    def record_observation(
        self, product_id: str, offer_url: str, store: str, ts: int,
        price_cents: int, total_cents: int, in_stock: bool | None, method: str,
        shipping_src: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO observations(product_id, offer_url, store, ts, price_cents, total_cents, in_stock, method,"
            " shipping_src) VALUES (?,?,?,?,?,?,?,?,?)",
            (product_id, offer_url, store, ts, price_cents, total_cents,
             None if in_stock is None else int(in_stock), method, shipping_src),
        )
        self.conn.commit()

    def daily_best(self, product_id: str, since_ts: int, until_ts: int, in_stock_only: bool) -> list[tuple[str, int]]:
        """Meilleur prix total (toutes boutiques) par jour, sur [since_ts, until_ts[."""
        stock_clause = "AND (in_stock IS NULL OR in_stock = 1)" if in_stock_only else ""
        rows = self.conn.execute(
            f"""SELECT date(ts, 'unixepoch', 'localtime') AS d, MIN(total_cents)
                FROM observations
                WHERE product_id = ? AND ts >= ? AND ts < ? {stock_clause}
                GROUP BY d ORDER BY d""",
            (product_id, since_ts, until_ts),
        ).fetchall()
        return [(d, int(p)) for d, p in rows]

    def history_summary(
        self, product_id: str, now: int, low_days: int, median_days: int, in_stock_only: bool
    ) -> tuple[int, int | None, int | None]:
        """(jours couverts, plus bas sur low_days, médiane des meilleurs prix journaliers sur median_days).

        Exclut les relevés >= now (le cycle en cours) pour comparer au passé uniquement.
        """
        low_rows = self.daily_best(product_id, now - low_days * 86400, now, in_stock_only)
        med_rows = self.daily_best(product_id, now - median_days * 86400, now, in_stock_only)
        low = min((p for _, p in low_rows), default=None)
        median = int(statistics.median(p for _, p in med_rows)) if med_rows else None
        return len(low_rows), low, median

    def latest_by_offer(self, product_id: str) -> list[LatestObservation]:
        rows = self.conn.execute(
            """SELECT o.offer_url, o.store, o.ts, o.price_cents, o.total_cents, o.in_stock, o.shipping_src
               FROM observations o
               JOIN (SELECT offer_url, MAX(ts) AS ts FROM observations WHERE product_id = ? GROUP BY offer_url) m
                 ON m.offer_url = o.offer_url AND m.ts = o.ts
               WHERE o.product_id = ?
               ORDER BY o.total_cents""",
            (product_id, product_id),
        ).fetchall()
        return [LatestObservation(u, s, t, p, tot, None if st is None else bool(st), src)
                for u, s, t, p, tot, st, src in rows]

    def alerts_for(self, product_id: str, limit: int = 20) -> list[tuple[int, int, str, str]]:
        """(ts, prix, boutique, règles) des dernières alertes envoyées."""
        return [(int(t), int(p), s or "", r or "") for t, p, s, r in self.conn.execute(
            "SELECT ts, price_cents, store, rules FROM alerts WHERE product_id = ? ORDER BY ts DESC LIMIT ?",
            (product_id, limit))]

    def last_check_ts(self) -> int | None:
        row = self.conn.execute("SELECT MAX(ts) FROM observations").fetchone()
        return int(row[0]) if row and row[0] else None

    def purge_product(self, product_id: str) -> None:
        for table in ("observations", "alert_state", "alerts", "external_stats"):
            self.conn.execute(f"DELETE FROM {table} WHERE product_id = ?", (product_id,))
        self.conn.commit()

    # --- état des alertes ---------------------------------------------------------------

    def get_alert_state(self, product_id: str) -> tuple[int, int] | None:
        row = self.conn.execute("SELECT price_cents, ts FROM alert_state WHERE product_id = ?", (product_id,)).fetchone()
        return (int(row[0]), int(row[1])) if row else None

    def set_alert_state(self, product_id: str, price_cents: int, ts: int, rules: str, store: str) -> None:
        self.conn.execute(
            "INSERT INTO alert_state(product_id, price_cents, ts, rules) VALUES (?,?,?,?)"
            " ON CONFLICT(product_id) DO UPDATE SET price_cents=excluded.price_cents, ts=excluded.ts, rules=excluded.rules",
            (product_id, price_cents, ts, rules),
        )
        self.conn.execute(
            "INSERT INTO alerts(product_id, ts, price_cents, store, rules) VALUES (?,?,?,?,?)",
            (product_id, ts, price_cents, store, rules),
        )
        self.conn.commit()

    def clear_alert_state(self, product_id: str) -> None:
        self.conn.execute("DELETE FROM alert_state WHERE product_id = ?", (product_id,))
        self.conn.commit()

    # --- échecs de récupération -------------------------------------------------------------

    def record_failure(self, offer_url: str, error: str, ts: int) -> tuple[int, bool]:
        """Incrémente le compteur d'échecs. Retourne (nb échecs consécutifs, déjà notifié)."""
        self.conn.execute(
            "INSERT INTO failures(offer_url, count, last_error, last_ts) VALUES (?,1,?,?)"
            " ON CONFLICT(offer_url) DO UPDATE SET count=count+1, last_error=excluded.last_error, last_ts=excluded.last_ts",
            (offer_url, error[:500], ts),
        )
        self.conn.commit()
        count, notified = self.conn.execute(
            "SELECT count, notified FROM failures WHERE offer_url = ?", (offer_url,)
        ).fetchone()
        return int(count), bool(notified)

    def mark_failure_notified(self, offer_url: str) -> None:
        self.conn.execute("UPDATE failures SET notified = 1 WHERE offer_url = ?", (offer_url,))
        self.conn.commit()

    def record_success(self, offer_url: str) -> bool:
        """Remet le compteur à zéro. Retourne True si une alerte d'échec avait été envoyée."""
        row = self.conn.execute("SELECT notified FROM failures WHERE offer_url = ?", (offer_url,)).fetchone()
        if row is None:
            return False
        self.conn.execute("DELETE FROM failures WHERE offer_url = ?", (offer_url,))
        self.conn.commit()
        return bool(row[0])

    def failures(self) -> list[tuple[str, int, str]]:
        return [(u, int(c), e or "") for u, c, e in self.conn.execute(
            "SELECT offer_url, count, last_error FROM failures ORDER BY count DESC")]

    # --- stats externes (Keepa...) ------------------------------------------------------------

    def get_external(self, product_id: str, source: str) -> ExternalStats | None:
        row = self.conn.execute(
            "SELECT ts, low_cents, avg_cents FROM external_stats WHERE product_id = ? AND source = ?",
            (product_id, source),
        ).fetchone()
        return ExternalStats(source, row[1], row[2], int(row[0])) if row else None

    def set_external(self, product_id: str, stats: ExternalStats) -> None:
        self.conn.execute(
            "INSERT INTO external_stats(product_id, source, ts, low_cents, avg_cents) VALUES (?,?,?,?,?)"
            " ON CONFLICT(product_id, source) DO UPDATE SET ts=excluded.ts, low_cents=excluded.low_cents,"
            " avg_cents=excluded.avg_cents",
            (product_id, stats.source, stats.ts, stats.low_cents, stats.avg_cents),
        )
        self.conn.commit()
