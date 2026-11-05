"""Exécution des cycles : planificateur en tâche de fond + déclenchements manuels (interface web).

Un seul cycle à la fois (verrou) ; chaque cycle ouvre ses propres connexion SQLite et Fetcher
(SQLite et Playwright ne se partagent pas entre threads).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from pathlib import Path

import yaml

from .catalog import Catalog
from .config import ConfigError, Settings
from .engine import Engine, ProductReport
from .fetch import Fetcher
from .notify import Notifier, build_notifiers
from .storage import Store

log = logging.getLogger(__name__)


def bootstrap_catalog(settings: Settings) -> None:
    """Premier démarrage : importe la watchlist YAML si la base est vide."""
    store = Store(settings.db_path)
    try:
        catalog = Catalog(store.conn)
        path = Path(settings.watchlist_path)
        if catalog.is_empty() and path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            count = catalog.import_dict(data)
            log.info("Watchlist importée depuis %s : %d produit(s)", path, count)
    finally:
        store.close()


class Runner:
    def __init__(self, settings: Settings, notifiers: list[Notifier] | None = None):
        self.settings = settings
        self.notifiers = notifiers if notifiers is not None else build_notifiers(settings.notifiers)
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self.running = False
        self.last_run_ts: float | None = None
        self.next_run_ts: float | None = None

    def run(self, product_ids: list[str] | None = None, notify: bool = True) -> list[ProductReport]:
        with self._lock:
            self.running = True
            store = Store(self.settings.db_path)
            try:
                products = Catalog(store.conn).load_products()
                if product_ids is not None:
                    products = [p for p in products if p.id in product_ids]
                with Fetcher(self.settings.domain_delay_seconds, self.settings.browser_enabled) as fetcher:
                    engine = Engine(self.settings, store, fetcher, self.notifiers)
                    reports = engine.run_cycle(products, notify=notify)
                log.info("Cycle terminé : %d produit(s), %d alerte(s)", len(reports), sum(r.notified for r in reports))
                return reports
            except ConfigError as exc:
                log.error("Watchlist invalide, cycle ignoré : %s", exc)
                return []
            finally:
                store.close()
                self.running = False
                self.last_run_ts = time.time()
                if product_ids:
                    self._pending.difference_update(product_ids)

    def trigger(self, product_ids: list[str]) -> bool:
        """Lance une vérification en arrière-plan. False si déjà en file pour ces produits."""
        new = [p for p in product_ids if p not in self._pending]
        if not new:
            return False
        self._pending.update(new)
        threading.Thread(target=self._safe_run, args=(new,), name="dealarr-manual", daemon=True).start()
        return True

    def is_pending(self, product_id: str) -> bool:
        return product_id in self._pending

    def _safe_run(self, product_ids: list[str] | None) -> None:
        try:
            self.run(product_ids)
        except Exception:
            log.exception("Cycle en échec")

    def loop(self, stop: threading.Event) -> None:
        """Boucle de planification (à lancer dans un thread ou au premier plan)."""
        while not stop.is_set():
            self._safe_run(None)
            jitter = 1 + random.uniform(-self.settings.jitter_pct, self.settings.jitter_pct) / 100
            wait = self.settings.interval_minutes * 60 * jitter
            self.next_run_ts = time.time() + wait
            log.info("Prochain cycle dans %d min", wait / 60)
            stop.wait(wait)

    def start_scheduler(self, stop: threading.Event) -> threading.Thread:
        thread = threading.Thread(target=self.loop, args=(stop,), name="dealarr-scheduler", daemon=True)
        thread.start()
        return thread
