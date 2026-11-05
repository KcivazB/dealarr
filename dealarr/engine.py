"""Cycle de vérification : récupère chaque offre, enregistre, évalue les règles, notifie."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .config import Offer, Product, Settings
from .extract import Extraction, extract, looks_blocked
from .fetch import FetchError, Fetcher
from .keepa import KEEPA_DOMAINS, fetch_stats
from .money import fmt, pct_diff
from .notify import Notification, Notifier, dispatch
from .rules import History, Signal, evaluate, is_suspicious, should_notify, should_rearm
from .sites import amazon_asin, domain_of, profile_for
from .storage import Store

log = logging.getLogger(__name__)

KEEPA_REFRESH_SECONDS = 24 * 3600


@dataclass
class OfferResult:
    offer: Offer
    extraction: Extraction | None = None
    error: str | None = None
    shipping_cents: int | None = None
    shipping_source: str = "inconnu"

    @property
    def total_cents(self) -> int | None:
        """Prix + port. Port inconnu compté à 0 (et signalé dans les notifications)."""
        if self.extraction is None or self.extraction.price_cents is None:
            return None
        return self.extraction.price_cents + (self.shipping_cents or 0)

    @property
    def shipping_label(self) -> str:
        if self.shipping_cents is None:
            return "port ?"
        if self.shipping_cents == 0:
            return "port offert"
        if self.shipping_source == "estimé":
            return f"dont ~{fmt(self.shipping_cents)} de port estimé"
        return f"dont {fmt(self.shipping_cents)} de port"


@dataclass
class ProductReport:
    product: Product
    results: list[OfferResult]
    best: OfferResult | None = None
    signals: list[Signal] = field(default_factory=list)
    notified: bool = False


class Engine:
    def __init__(self, settings: Settings, store: Store, fetcher: Fetcher, notifiers: list[Notifier], clock=time.time):
        self.settings = settings
        self.store = store
        self.fetcher = fetcher
        self.notifiers = notifiers
        self.clock = clock

    # --- cycle ------------------------------------------------------------------------------

    def run_cycle(self, products: list[Product], notify: bool = True) -> list[ProductReport]:
        reports = []
        for product in products:
            if not product.enabled:
                continue
            try:
                reports.append(self.check_product(product, notify=notify))
            except Exception:  # un produit en erreur ne doit pas bloquer les autres
                log.exception("Erreur inattendue sur %s", product.id)
        return reports

    def check_product(self, product: Product, notify: bool = True) -> ProductReport:
        now = int(self.clock())
        results = [self._check_offer(product, offer, now, notify) for offer in product.offers]
        report = ProductReport(product, results)

        rules = product.rules
        candidates = [
            r for r in results
            if r.total_cents is not None and not (rules.require_in_stock and r.extraction.in_stock is False)
        ]
        if not candidates:
            log.info("%s : aucune offre disponible", product.id)
            return report
        best = min(candidates, key=lambda r: r.total_cents)
        report.best = best
        price = best.total_cents

        hist = self._history(product, now)
        report.signals = evaluate(price, product, hist)
        state = self.store.get_alert_state(product.id)

        if not report.signals:
            if should_rearm(state, now, rules):
                self.store.clear_alert_state(product.id)
            log.info("%s : %s chez %s, rien à signaler", product.id, fmt(price), best.offer.store)
            return report

        if not should_notify(price, state, rules):
            log.info("%s : %s, déjà signalé à %s", product.id, fmt(price), fmt(state[0]))
            return report

        notification = self._deal_notification(product, best, results, report.signals, hist)
        if notify:
            if not dispatch(self.notifiers, notification):
                log.error("%s : aucune notification n'a pu être envoyée, nouvel essai au prochain cycle", product.id)
                return report
            self.store.set_alert_state(product.id, price, now, ",".join(s.rule for s in report.signals), best.offer.store)
        report.notified = True
        return report

    # --- offres ------------------------------------------------------------------------------

    def _check_offer(self, product: Product, offer: Offer, now: int, notify: bool) -> OfferResult:
        profile = profile_for(offer.url)
        use_browser = offer.browser if offer.browser is not None else bool(profile and profile.needs_browser)
        result = OfferResult(offer)
        try:
            extraction = self._fetch_extract(offer, profile, use_browser)
        except FetchError as exc:
            # Pas de nouvelle tentative pour contourner un anti-bot : on signale l'échec.
            result.error = str(exc)
            self._failure(product, offer, str(exc), now, notify)
            return result
        return self._success(product, offer, extraction, now, notify, result)

    def _fetch_extract(self, offer: Offer, profile, browser: bool) -> Extraction:
        html = self.fetcher.get(offer.url, browser=browser)
        extraction = extract(html, profile, offer.selector)
        if not extraction.ok:
            if looks_blocked(html):
                raise FetchError("page anti-bot", blocked=True)
            raise FetchError("prix introuvable dans la page")
        return extraction

    def _success(self, product: Product, offer: Offer, extraction: Extraction, now: int, notify: bool, result: OfferResult) -> OfferResult:
        result.extraction = extraction
        result.shipping_cents, result.shipping_source = offer.shipping_for(extraction.price_cents, extraction.shipping_cents)
        total = result.total_cents
        self.store.record_observation(
            product.id, offer.url, offer.store, now, extraction.price_cents, total, extraction.in_stock, extraction.method,
            result.shipping_source,
        )
        stock = {True: "en stock", False: "rupture", None: "stock ?"}[extraction.in_stock]
        log.info("  %-16s %10s  %-26s %s  (%s)", offer.store, fmt(total),
                 f"{result.shipping_label} [{result.shipping_source}]", stock, extraction.method)
        if self.store.record_success(offer.url) and notify:
            dispatch(self.notifiers, Notification(
                title=f"✅ {offer.store} à nouveau lisible",
                message=f"{product.name} : suivi rétabli ({fmt(total)}).",
                priority=1, tags=("white_check_mark",), url=offer.url, kind="system",
            ))
        return result

    def _failure(self, product: Product, offer: Offer, error: str, now: int, notify: bool) -> None:
        log.warning("  %-14s ÉCHEC : %s", offer.store, error)
        count, already = self.store.record_failure(offer.url, error, now)
        if count >= self.settings.failure_threshold and not already and notify:
            sent = dispatch(self.notifiers, Notification(
                title=f"⚠️ Suivi en échec : {offer.store}",
                message=(f"{product.name} — {count} échecs consécutifs.\nDernière erreur : {error}\n"
                         f"Vérifie l'URL ou teste : dealarr probe <url>"),
                priority=2, tags=("warning",), url=offer.url, kind="system",
            ))
            if sent:
                self.store.mark_failure_notified(offer.url)

    # --- historique --------------------------------------------------------------------------

    def _history(self, product: Product, now: int) -> History:
        r = product.rules
        days, low, median = self.store.history_summary(
            product.id, now, r.lowest_window_days, r.median_window_days, r.require_in_stock
        )
        ext = self._external(product, now)
        return History(
            days_covered=days, low_cents=low, median_cents=median,
            external_low_cents=ext.low_cents if ext else None,
            external_avg_cents=ext.avg_cents if ext else None,
            external_source=ext.source if ext else None,
        )

    def _external(self, product: Product, now: int):
        if not self.settings.keepa_api_key:
            return None
        amazon = next((o for o in product.offers if amazon_asin(o.url)), None)
        if amazon is None:
            return None
        cached = self.store.get_external(product.id, "Keepa")
        if cached and now - cached.ts < KEEPA_REFRESH_SECONDS:
            return cached
        stats = fetch_stats(self.settings.keepa_api_key, amazon_asin(amazon.url), domain_of(amazon.url))
        if stats:
            self.store.set_external(product.id, stats)
            return stats
        return cached

    # --- message -------------------------------------------------------------------------------

    def _deal_notification(self, product: Product, best: OfferResult, results: list[OfferResult],
                           signals: list[Signal], hist: History) -> Notification:
        price = best.total_cents
        lines = [f"• {s.text}" for s in signals]
        if is_suspicious(price, hist):
            lines.insert(0, "⚠️ Prix anormalement bas : vérifie le vendeur / l'annonce avant d'acheter.")
        if best.shipping_cents is None:
            lines.append(f"Prix article {fmt(best.extraction.price_cents)}, frais de port inconnus (à vérifier)")
        elif best.shipping_source == "estimé":
            lines.append(f"{fmt(best.extraction.price_cents)} + ~{fmt(best.shipping_cents)} de port estimé (réel inconnu, à vérifier)")
        elif best.shipping_cents == 0:
            lines.append(f"Livraison offerte ({best.shipping_source})")
        else:
            lines.append(f"{fmt(best.extraction.price_cents)} + {fmt(best.shipping_cents)} de port ({best.shipping_source})")

        refs = []
        if hist.low_cents is not None:
            refs.append(f"plus bas {product.rules.lowest_window_days} j : {fmt(hist.low_cents)}")
        if hist.median_cents is not None:
            refs.append(f"médiane : {fmt(hist.median_cents)}")
        if refs:
            lines.append("Historique local (" + f"{hist.days_covered} j) — " + ", ".join(refs))

        others = sorted((r for r in results if r is not best), key=lambda r: (r.total_cents is None, r.total_cents or 0))
        if others:
            lines.append("")
            lines.append("Ailleurs :")
            for r in others:
                if r.total_cents is None:
                    lines.append(f"  {r.offer.store} : indisponible ({r.error})")
                else:
                    extra = ", rupture" if r.extraction.in_stock is False else ""
                    lines.append(f"  {r.offer.store} : {fmt(r.total_cents)} (+{pct_diff(r.total_cents, price):.0f} %, "
                                 f"{r.shipping_label}{extra})")

        actions: list[tuple[str, str]] = [(f"Voir sur {best.offer.store}", best.offer.url)]
        amazon = next((o for o in product.offers if amazon_asin(o.url)), None)
        if amazon:
            asin = amazon_asin(amazon.url)
            actions.append(("Historique Keepa", f"https://keepa.com/#!product/{KEEPA_DOMAINS.get(domain_of(amazon.url), 4)}-{asin}"))
            actions.append(("CamelCamelCamel", f"https://fr.camelcamelcamel.com/product/{asin}"))

        return Notification(
            title=f"{product.name} : {fmt(price)} chez {best.offer.store}",
            message="\n".join(lines),
            priority=max(s.priority for s in signals),
            tags=("moneybag",) + (("chart_with_downwards_trend",) if any(s.rule != "target" for s in signals) else ()),
            url=best.offer.url,
            actions=tuple(actions),
        )
