"""Watchlist stockée en base : produits, offres, politiques de port, réglages.

La représentation pivot est le même dictionnaire que le YAML (`to_dict`) : toute écriture est
validée en le repassant dans `parse_watchlist`, et annulée (rollback) s'il est invalide.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from .config import ConfigError, Offer, Product, RuleSettings, _rules, money_input, parse_watchlist
from .sites import canonical_url, domain_of

_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    target_cents INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1,
    rules_json TEXT NOT NULL DEFAULT '{}',
    created_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    store TEXT,
    shipping_cents INTEGER,
    free_above_cents INTEGER,
    selector TEXT,
    browser INTEGER,
    UNIQUE (product_id, url)
);
CREATE TABLE IF NOT EXISTS store_policies (
    domain TEXT PRIMARY KEY,
    cost_cents INTEGER,
    free_above_cents INTEGER,
    tiers_json TEXT NOT NULL DEFAULT '[]',
    note TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
"""


def _eur(cents: int | None) -> str | None:
    return None if cents is None else f"{cents / 100:.2f}"


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text or "produit")[:50].strip("-")


@dataclass(frozen=True)
class OfferRow:
    id: int
    url: str
    store: str | None
    shipping_cents: int | None
    free_above_cents: int | None
    selector: str | None
    browser: bool | None


@dataclass(frozen=True)
class StorePolicyRow:
    domain: str
    cost_cents: int | None
    free_above_cents: int | None
    tiers: tuple[tuple[int, int], ...]
    note: str | None


class Catalog:
    def __init__(self, conn):
        self.conn = conn
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)

    # --- lecture ------------------------------------------------------------------------------

    def is_empty(self) -> bool:
        return self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0

    def load_products(self) -> list[Product]:
        return parse_watchlist(self.to_dict())

    def product(self, product_id: str) -> Product | None:
        return next((p for p in self.load_products() if p.id == product_id), None)

    def rule_overrides(self, product_id: str) -> dict:
        row = self.conn.execute("SELECT rules_json FROM products WHERE id = ?", (product_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def offers(self, product_id: str) -> list[OfferRow]:
        rows = self.conn.execute(
            "SELECT id, url, store, shipping_cents, free_above_cents, selector, browser FROM offers"
            " WHERE product_id = ? ORDER BY id", (product_id,))
        return [OfferRow(i, u, s, sh, fa, sel, None if b is None else bool(b)) for i, u, s, sh, fa, sel, b in rows]

    def products_by_url(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for url, pid in self.conn.execute("SELECT url, product_id FROM offers"):
            result.setdefault(url, []).append(pid)
        return result

    def store_policies(self) -> list[StorePolicyRow]:
        rows = self.conn.execute(
            "SELECT domain, cost_cents, free_above_cents, tiers_json, note FROM store_policies ORDER BY domain")
        return [StorePolicyRow(d, c, f, tuple(tuple(t) for t in json.loads(tj)), n) for d, c, f, tj, n in rows]

    def tracked_domains(self) -> list[str]:
        return sorted({domain_of(u) for (u,) in self.conn.execute("SELECT url FROM offers")})

    def _setting(self, key: str, default=None):
        row = self.conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def defaults(self) -> dict:
        return self._setting("defaults", {})

    def effective_defaults(self) -> RuleSettings:
        return _rules(self.defaults(), RuleSettings(), "defaults")

    def default_shipping(self) -> int | None:
        return self._setting("default_shipping_cents")

    def to_dict(self) -> dict:
        data: dict = {"defaults": self.defaults()}
        if (ds := self.default_shipping()) is not None:
            data["default_shipping"] = _eur(ds)
        stores = {}
        for p in self.store_policies():
            entry: dict = {}
            if p.cost_cents is not None:
                entry["cost"] = _eur(p.cost_cents)
            if p.free_above_cents is not None:
                entry["free_above"] = _eur(p.free_above_cents)
            if p.tiers:
                entry["tiers"] = [{"below": _eur(b), "cost": _eur(c)} for b, c in p.tiers]
            stores[p.domain] = entry
        if stores:
            data["stores"] = stores

        products = []
        rows = self.conn.execute(
            "SELECT id, name, target_cents, enabled, rules_json FROM products ORDER BY created_ts, id").fetchall()
        for pid, name, target, enabled, rules_json in rows:
            offers = []
            for o in self.offers(pid):
                entry = {"url": o.url}
                if o.store:
                    entry["store"] = o.store
                if o.shipping_cents is not None:
                    entry["shipping"] = _eur(o.shipping_cents)
                if o.free_above_cents is not None:
                    entry["free_above"] = _eur(o.free_above_cents)
                if o.selector:
                    entry["selector"] = o.selector
                if o.browser is not None:
                    entry["browser"] = o.browser
                offers.append(entry)
            product: dict = {"id": pid, "name": name, "offers": offers}
            if target is not None:
                product["target_price"] = _eur(target)
            if not enabled:
                product["enabled"] = False
            rules = json.loads(rules_json)
            if rules:
                product["rules"] = rules
            products.append(product)
        data["products"] = products
        return data

    def preview_offer(self, url: str) -> Offer:
        """Offre telle qu'elle serait suivie (nom de boutique, politique de port résolue)."""
        data = self.to_dict()
        data["products"] = [{"id": "preview", "offers": [url]}]
        return parse_watchlist(data)[0].offers[0]

    # --- écriture (transactionnelle + validée) -------------------------------------------------

    def _tx(self, fn: Callable[[], object]):
        try:
            with self.conn:  # commit si OK, rollback sur exception
                result = fn()
                parse_watchlist(self.to_dict())
                return result
        except Exception as exc:  # sqlite3.IntegrityError, etc.
            if isinstance(exc, ConfigError):
                raise
            if "UNIQUE" in str(exc):
                raise ConfigError("Cette URL est déjà suivie pour ce produit") from exc
            raise

    def _unique_id(self, name: str) -> str:
        base, n = slugify(name), 1
        candidate = base
        while self.conn.execute("SELECT 1 FROM products WHERE id = ?", (candidate,)).fetchone():
            n += 1
            candidate = f"{base[:46]}-{n}"
        return candidate

    def create_product(self, name: str, target_cents: int | None, urls: list[str], rules: dict | None = None) -> str:
        name = name.strip()
        if not name:
            raise ConfigError("Le nom est obligatoire")
        urls = [u.strip() for u in urls if u.strip()]
        if not urls:
            raise ConfigError("Au moins une URL est requise")

        def do():
            pid = self._unique_id(name)
            self.conn.execute(
                "INSERT INTO products(id, name, target_cents, enabled, rules_json, created_ts) VALUES (?,?,?,?,?,?)",
                (pid, name, target_cents, 1, json.dumps(rules or {}), int(time.time())))
            for url in dict.fromkeys(canonical_url(u) for u in urls):
                self._insert_offer(pid, url)
            return pid

        return self._tx(do)

    def update_product(self, product_id: str, name: str, target_cents: int | None, enabled: bool, rules: dict) -> None:
        if not name.strip():
            raise ConfigError("Le nom est obligatoire")
        self._tx(lambda: self.conn.execute(
            "UPDATE products SET name = ?, target_cents = ?, enabled = ?, rules_json = ? WHERE id = ?",
            (name.strip(), target_cents, int(enabled), json.dumps(rules), product_id)))

    def delete_product(self, product_id: str) -> None:
        self._tx(lambda: self.conn.execute("DELETE FROM products WHERE id = ?", (product_id,)))

    def _insert_offer(self, product_id: str, url: str, store: str | None = None, shipping_cents: int | None = None,
                      free_above_cents: int | None = None, selector: str | None = None,
                      browser: bool | None = None) -> None:
        if not url.startswith(("https://", "http://")):
            raise ConfigError(f"URL invalide : {url}")
        self.conn.execute(
            "INSERT INTO offers(product_id, url, store, shipping_cents, free_above_cents, selector, browser)"
            " VALUES (?,?,?,?,?,?,?)",
            (product_id, canonical_url(url), store or None, shipping_cents, free_above_cents, selector or None,
             None if browser is None else int(browser)))

    def add_offer(self, product_id: str, url: str, **kwargs) -> None:
        if not self.conn.execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone():
            raise ConfigError("Produit introuvable")
        self._tx(lambda: self._insert_offer(product_id, url.strip(), **kwargs))

    def update_offer(self, product_id: str, offer_id: int, store: str | None, shipping_cents: int | None,
                     free_above_cents: int | None, selector: str | None, browser: bool | None) -> None:
        self._tx(lambda: self.conn.execute(
            "UPDATE offers SET store = ?, shipping_cents = ?, free_above_cents = ?, selector = ?, browser = ?"
            " WHERE id = ? AND product_id = ?",
            (store or None, shipping_cents, free_above_cents, selector or None,
             None if browser is None else int(browser), offer_id, product_id)))

    def delete_offer(self, product_id: str, offer_id: int) -> None:
        def do():
            self.conn.execute("DELETE FROM offers WHERE id = ? AND product_id = ?", (offer_id, product_id))
            if not self.conn.execute("SELECT 1 FROM offers WHERE product_id = ?", (product_id,)).fetchone():
                raise ConfigError("Un produit doit garder au moins une offre (supprime plutôt le produit)")
        self._tx(do)

    def set_store_policy(self, domain: str, cost_cents: int | None, free_above_cents: int | None,
                         tiers: list[tuple[int, int]] | tuple = (), note: str | None = None) -> None:
        domain = domain.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
        domain = domain.removeprefix("www.")
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z0-9-]+", domain):
            raise ConfigError(f"Domaine invalide : {domain!r}")
        self._tx(lambda: self.conn.execute(
            "INSERT INTO store_policies(domain, cost_cents, free_above_cents, tiers_json, note) VALUES (?,?,?,?,?)"
            " ON CONFLICT(domain) DO UPDATE SET cost_cents=excluded.cost_cents,"
            " free_above_cents=excluded.free_above_cents, tiers_json=excluded.tiers_json, note=excluded.note",
            (domain, cost_cents, free_above_cents, json.dumps(sorted(tiers)), note or None)))

    def delete_store_policy(self, domain: str) -> None:
        self._tx(lambda: self.conn.execute("DELETE FROM store_policies WHERE domain = ?", (domain,)))

    def _put_setting(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value_json) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            (key, json.dumps(value)))

    def set_defaults(self, defaults: dict, default_shipping_cents: int | None) -> None:
        def do():
            self._put_setting("defaults", defaults)
            self._put_setting("default_shipping_cents", default_shipping_cents)
        self._tx(do)

    # --- import / export YAML -------------------------------------------------------------------

    def import_dict(self, data: dict) -> int:
        """Remplace toute la watchlist par `data` (format YAML). Retourne le nombre de produits."""
        products = parse_watchlist(data)  # validation complète avant d'écrire quoi que ce soit

        def do():
            for table in ("offers", "products", "store_policies", "settings"):
                self.conn.execute(f"DELETE FROM {table}")
            self._put_setting("defaults", dict(data.get("defaults") or {}))
            self._put_setting("default_shipping_cents", money_input(data.get("default_shipping"), "default_shipping"))
            for domain, raw in (data.get("stores") or {}).items():
                raw = raw or {}
                tiers = [(money_input(t["below"]), money_input(t["cost"])) for t in raw.get("tiers") or []]
                self.conn.execute(
                    "INSERT INTO store_policies(domain, cost_cents, free_above_cents, tiers_json) VALUES (?,?,?,?)",
                    (str(domain).lower().removeprefix("www."), money_input(raw.get("cost")),
                     money_input(raw.get("free_above")), json.dumps(sorted(tiers))))
            now = int(time.time())
            for i, raw in enumerate(data.get("products") or []):
                pid = str(raw["id"]).strip().lower()
                self.conn.execute(
                    "INSERT INTO products(id, name, target_cents, enabled, rules_json, created_ts) VALUES (?,?,?,?,?,?)",
                    (pid, str(raw.get("name") or pid), money_input(raw.get("target_price")),
                     int(bool(raw.get("enabled", True))), json.dumps(dict(raw.get("rules") or {})), now + i))
                for o in raw.get("offers") or []:
                    o = {"url": o} if isinstance(o, str) else o
                    self._insert_offer(pid, str(o["url"]).strip(), o.get("store"), money_input(o.get("shipping")),
                                       money_input(o.get("free_above")), o.get("selector"), o.get("browser"))
            return len(products)

        return self._tx(do)
