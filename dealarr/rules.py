"""Règles de déclenchement et anti-spam."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Product, RuleSettings
from .money import fmt, pct_diff


@dataclass(frozen=True)
class History:
    days_covered: int
    low_cents: int | None  # plus bas local (fenêtre lowest_window_days), hors cycle courant
    median_cents: int | None  # médiane locale (fenêtre median_window_days)
    external_low_cents: int | None = None  # ex. Keepa : plus bas 90 j
    external_avg_cents: int | None = None  # ex. Keepa : moyenne 90 j
    external_source: str | None = None


@dataclass(frozen=True)
class Signal:
    rule: str  # target | lowest | drop
    text: str
    priority: int  # 1..5 (échelle ntfy)


def evaluate(price: int, product: Product, hist: History) -> list[Signal]:
    r = product.rules
    tol = 1 + r.tolerance_pct / 100
    signals: list[Signal] = []

    # 1. Seuil de prix (fonctionne dès le premier relevé)
    if product.target_cents is not None and price <= product.target_cents * tol:
        if price <= product.target_cents:
            signals.append(Signal("target", f"Sous ton seuil de {fmt(product.target_cents)}", 4))
        else:
            gap = pct_diff(price, product.target_cents)
            signals.append(Signal("target", f"Proche de ton seuil de {fmt(product.target_cents)} (+{gap:.1f} %)", 3))

    has_local = hist.days_covered >= r.min_history_days

    # Prix "habituel" : médiane locale, sinon moyenne externe.
    typical = (hist.median_cents if has_local else None) or hist.external_avg_cents

    # 2. Plus bas historique (local et/ou externe), avec la même tolérance.
    #    Ne compte que si le prix est réellement sous le prix habituel : un prix plat est
    #    toujours "au plus bas" et ne doit pas déclencher.
    lows = []
    if has_local and hist.low_cents is not None:
        lows.append((hist.low_cents, f"{r.lowest_window_days} j"))
    if hist.external_low_cents is not None:
        lows.append((hist.external_low_cents, f"90 j {hist.external_source}"))
    if lows and typical is not None and price < typical / tol:
        low, label = min(lows, key=lambda x: x[0])
        if price < low:
            signals.append(Signal("lowest", f"Nouveau plus bas (précédent {fmt(low)} sur {label})", 4))
        elif price <= low * tol:
            signals.append(Signal("lowest", f"Proche du plus bas {label} : {fmt(low)} (+{pct_diff(price, low):.1f} %)", 3))

    # 3. Vraie baisse par rapport au prix "habituel"
    refs = []
    if has_local and hist.median_cents is not None:
        refs.append((hist.median_cents, f"médiane {r.median_window_days} j"))
    if hist.external_avg_cents is not None:
        refs.append((hist.external_avg_cents, f"moyenne 90 j {hist.external_source}"))
    for ref, label in refs:
        if price <= ref * (1 - r.drop_pct / 100):
            signals.append(Signal("drop", f"{pct_diff(price, ref):.0f} % vs {label} ({fmt(ref)})", 3))
            break

    return signals


def should_notify(price: int, state: tuple[int, int] | None, rules: RuleSettings) -> bool:
    """Une seule alerte par épisode de promo, sauf si le prix baisse encore significativement."""
    if state is None:
        return True
    last_price, _ = state
    return price <= last_price * (1 - rules.renotify_drop_pct / 100)


def should_rearm(state: tuple[int, int] | None, now: int, rules: RuleSettings) -> bool:
    """Plus aucune règle ne déclenche : on réarme après le cooldown (évite le yo-yo autour du seuil)."""
    return state is not None and now - state[1] >= rules.cooldown_hours * 3600


def is_suspicious(price: int, hist: History) -> bool:
    """Prix anormalement bas (erreur d'extraction, vendeur marketplace douteux...)."""
    ref = hist.median_cents or hist.external_avg_cents
    return ref is not None and price < ref * 0.35
