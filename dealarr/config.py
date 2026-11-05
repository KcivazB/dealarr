"""Chargement de la watchlist (YAML) et des réglages (variables d'environnement)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Mapping

import yaml

from .money import parse_price
from .sites import canonical_url, domain_of, store_name


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RuleSettings:
    tolerance_pct: float = 5.0  # marge au-dessus du seuil / du plus bas qui déclenche quand même
    drop_pct: float = 15.0  # baisse vs médiane pour la règle "vraie baisse"
    lowest_window_days: int = 90
    median_window_days: int = 30
    min_history_days: int = 7  # jours d'historique local avant d'activer plus-bas / médiane
    require_in_stock: bool = True
    renotify_drop_pct: float = 2.0  # re-notifier seulement si le prix baisse encore de X %
    cooldown_hours: float = 12.0  # délai avant de "réarmer" une alerte après remontée du prix


@dataclass(frozen=True)
class ShippingPolicy:
    cost_cents: int | None = None  # frais de port standard de la boutique
    free_above_cents: int | None = None  # livraison offerte à partir de ce montant
    tiers: tuple[tuple[int, int], ...] = ()  # (prix article < seuil, coût), triés ; prioritaires sur cost

    def cost_for(self, price_cents: int) -> int | None:
        for below, cost in self.tiers:
            if price_cents < below:
                return cost
        return self.cost_cents


@dataclass(frozen=True)
class Offer:
    url: str
    store: str
    shipping_cents: int | None = None  # forcé dans la watchlist (prioritaire sur tout le reste)
    policy: ShippingPolicy = ShippingPolicy()
    selector: str | None = None
    browser: bool | None = None
    estimated_shipping_cents: int | None = None  # repli quand le port est inconnu (default_shipping)

    def shipping_for(self, price_cents: int, page_shipping_cents: int | None) -> tuple[int | None, str]:
        """Frais de port à ajouter et leur origine : config | offert | page | boutique | inconnu."""
        if self.shipping_cents is not None:
            return self.shipping_cents, "config"
        if self.policy.free_above_cents is not None and price_cents >= self.policy.free_above_cents:
            return 0, "offert"
        if page_shipping_cents is not None:
            return page_shipping_cents, "page"
        if (cost := self.policy.cost_for(price_cents)) is not None:
            return cost, "boutique"
        if self.estimated_shipping_cents is not None:
            return self.estimated_shipping_cents, "estimé"
        return None, "inconnu"


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    offers: tuple[Offer, ...]
    target_cents: int | None
    rules: RuleSettings
    enabled: bool = True


_RULE_FIELDS = {f.name: f.type for f in fields(RuleSettings)}
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _rules(raw: object, base: RuleSettings, where: str) -> RuleSettings:
    if raw is None:
        return base
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where}: 'rules' doit être un dictionnaire")
    unknown = set(raw) - set(_RULE_FIELDS)
    if unknown:
        raise ConfigError(f"{where}: règles inconnues {sorted(unknown)} (connues : {sorted(_RULE_FIELDS)})")
    values = {}
    for key, value in raw.items():
        kind = _RULE_FIELDS[key]
        try:
            values[key] = value if kind == "bool" else (int(value) if kind == "int" else float(value))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{where}: valeur invalide pour {key}: {value!r}") from exc
        if kind == "bool" and not isinstance(value, bool):
            raise ConfigError(f"{where}: {key} doit valoir true/false")
    return replace(base, **values)


_ZERO = re.compile(r"\s*0+(?:[.,]0*)?\s*€?\s*")


def money_input(value: object, where: str = "montant") -> int | None:
    """Montant saisi (YAML ou formulaire) → centimes. Vide → None, '0' → 0, invalide → ConfigError."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _money(value, where)


def _money(value: object, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigError(f"{where}: montant invalide {value!r}")
    if value == 0 or _ZERO.fullmatch(str(value)):
        return 0
    cents = parse_price(value)
    if cents is None:
        raise ConfigError(f"{where}: montant invalide {value!r}")
    return cents


def _policy(raw: object, where: str, base: ShippingPolicy = ShippingPolicy()) -> ShippingPolicy:
    if raw is None:
        return base
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where}: doit être un dictionnaire (cost, free_above)")
    unknown = set(raw) - {"cost", "free_above", "tiers"}
    if unknown:
        raise ConfigError(f"{where}: clés inconnues {sorted(unknown)} (attendu : cost, free_above, tiers)")
    tiers = base.tiers
    if "tiers" in raw:
        try:
            tiers = tuple(sorted((_money(t["below"], where), _money(t["cost"], where)) for t in raw["tiers"]))
        except (TypeError, KeyError) as exc:
            raise ConfigError(f"{where}: tiers attend une liste de {{below, cost}}") from exc
    return ShippingPolicy(
        cost_cents=_money(raw["cost"], where) if "cost" in raw else base.cost_cents,
        free_above_cents=_money(raw["free_above"], where) if "free_above" in raw else base.free_above_cents,
        tiers=tiers,
    )


def _store_policies(raw: object) -> dict[str, ShippingPolicy]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ConfigError("'stores' doit être un dictionnaire domaine → {cost, free_above}")
    return {str(domain).lower().removeprefix("www."): _policy(p, f"stores.{domain}") for domain, p in raw.items()}


def _policy_for(url: str, policies: dict[str, ShippingPolicy]) -> ShippingPolicy:
    host = domain_of(url)
    for domain, policy in policies.items():
        if host == domain or host.endswith("." + domain):
            return policy
    return ShippingPolicy()


def parse_watchlist(data: object) -> list[Product]:
    if not isinstance(data, Mapping):
        raise ConfigError("La watchlist doit contenir un dictionnaire avec 'products'")
    defaults = _rules(data.get("defaults"), RuleSettings(), "defaults")
    policies = _store_policies(data.get("stores"))
    default_shipping = _money(data.get("default_shipping"), "default_shipping")
    raw_products = data.get("products") or []
    if not isinstance(raw_products, list):
        raise ConfigError("'products' doit être une liste")

    products: list[Product] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_products):
        where = f"products[{i}]"
        if not isinstance(raw, Mapping):
            raise ConfigError(f"{where}: entrée invalide")
        pid = str(raw.get("id") or "").strip().lower()
        if not _ID.match(pid):
            raise ConfigError(f"{where}: 'id' requis (minuscules, chiffres, - ou _)")
        if pid in seen:
            raise ConfigError(f"{where}: id '{pid}' en double")
        seen.add(pid)
        where = f"produit '{pid}'"

        offers = []
        for j, o in enumerate(raw.get("offers") or []):
            if isinstance(o, str):
                o = {"url": o}
            url = str(o.get("url") or "").strip()
            if not url.startswith(("https://", "http://")):
                raise ConfigError(f"{where}, offre {j}: url http(s) requise")
            browser = o.get("browser")
            unknown = set(o) - {"url", "store", "shipping", "free_above", "selector", "browser"}
            if unknown:
                raise ConfigError(f"{where}, offre {j}: clés inconnues {sorted(unknown)}")
            store_policy = _policy_for(url, policies)
            offers.append(
                Offer(
                    url=canonical_url(url),
                    store=str(o.get("store") or store_name(url)),
                    shipping_cents=_money(o.get("shipping"), where),
                    policy=_policy({"free_above": o["free_above"]}, where, store_policy) if "free_above" in o else store_policy,
                    selector=o.get("selector"),
                    browser=bool(browser) if browser is not None else None,
                    estimated_shipping_cents=default_shipping,
                )
            )
        if not offers:
            raise ConfigError(f"{where}: au moins une offre (url) est requise")

        products.append(
            Product(
                id=pid,
                name=str(raw.get("name") or pid),
                offers=tuple(offers),
                target_cents=_money(raw.get("target_price"), where),
                rules=_rules(raw.get("rules"), defaults, where),
                enabled=bool(raw.get("enabled", True)),
            )
        )
    return products


def load_watchlist(path: Path) -> list[Product]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"Watchlist introuvable : {path}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML invalide dans {path}: {exc}") from exc
    return parse_watchlist(data or {})


@dataclass(frozen=True)
class Settings:
    watchlist_path: Path = Path("watchlist.yaml")
    db_path: Path = Path("data/dealarr.db")
    interval_minutes: int = 180
    jitter_pct: float = 15.0
    domain_delay_seconds: tuple[float, float] = (4.0, 10.0)
    failure_threshold: int = 3
    notifiers: tuple[str, ...] = ("console",)
    browser_enabled: bool = False
    keepa_api_key: str | None = None
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        def get(name: str, default: str) -> str:
            return env.get(name, "").strip() or default

        try:
            delay = [float(x) for x in get("DEALARR_DOMAIN_DELAY", "4-10").split("-")]
            return cls(
                watchlist_path=Path(get("DEALARR_WATCHLIST", "watchlist.yaml")),
                db_path=Path(get("DEALARR_DB", "data/dealarr.db")),
                interval_minutes=int(get("DEALARR_INTERVAL_MINUTES", "180")),
                jitter_pct=float(get("DEALARR_JITTER_PCT", "15")),
                domain_delay_seconds=(delay[0], delay[-1]),
                failure_threshold=int(get("DEALARR_FAILURE_THRESHOLD", "3")),
                notifiers=tuple(n.strip().lower() for n in get("DEALARR_NOTIFIERS", "console").split(",") if n.strip()),
                browser_enabled=get("DEALARR_BROWSER", "false").lower() in ("1", "true", "yes"),
                keepa_api_key=env.get("KEEPA_API_KEY", "").strip() or None,
                log_level=get("DEALARR_LOG_LEVEL", "INFO").upper(),
            )
        except ValueError as exc:
            raise ConfigError(f"Variable d'environnement invalide : {exc}") from exc
