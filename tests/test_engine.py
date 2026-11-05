import json

import httpx
import pytest

from dealarr.config import ConfigError, Settings, parse_watchlist
from dealarr.engine import Engine
from dealarr.fetch import FetchError
from dealarr.notify import Notification
from dealarr.notify.ntfy import NtfyNotifier
from dealarr.rules import History, evaluate
from dealarr.storage import Store

DAY = 86400
T0 = 1_760_000_000


def page(price, stock="InStock"):
    return (f'<script type="application/ld+json">{{"@type":"Product","name":"Jeu",'
            f'"offers":{{"price":"{price}","priceCurrency":"EUR","availability":"https://schema.org/{stock}"}}}}</script>')


class FakeFetcher:
    def __init__(self):
        self.pages: dict[str, object] = {}

    def get(self, url, browser=False):
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return value


class Recorder:
    name = "recorder"

    def __init__(self):
        self.sent: list[Notification] = []

    def send(self, n):
        self.sent.append(n)

    def deals(self):
        return [n for n in self.sent if n.kind == "deal"]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


A = "https://www.philibertnet.com/fr/jeu.html"
B = "https://www.ludum.fr/jeu"


def make(products_yaml, settings=None):
    products = parse_watchlist(products_yaml)
    fetcher, rec, clock = FakeFetcher(), Recorder(), Clock(T0)
    engine = Engine(settings or Settings(), Store(":memory:"), fetcher, [rec], clock=clock)
    return engine, products, fetcher, rec, clock


def base(target=40, **rules):
    return {"products": [{"id": "jeu", "name": "Jeu", "target_price": target, "offers": [A, {"url": B, "shipping": "5"}],
                          "rules": rules or None}]}


# --- règles ----------------------------------------------------------------------------------

def test_target_tolerance():
    p = parse_watchlist(base(target=40))[0]
    empty = History(0, None, None)
    assert evaluate(4200, p, empty)[0].priority == 3  # 42 = 40 + 5 % → proche du seuil
    assert evaluate(3999, p, empty)[0].priority == 4


def test_target_tolerance_boundary():
    p = parse_watchlist(base(target=40))[0]
    assert evaluate(4201, p, History(0, None, None)) == []


def test_history_rules_need_min_days():
    p = parse_watchlist(base(target=None))[0]
    assert evaluate(3000, p, History(3, 4000, 5000)) == []
    rules = {s.rule for s in evaluate(3000, p, History(10, 4000, 5000))}
    assert rules == {"lowest", "drop"}


def test_external_stats_used_without_local_history():
    p = parse_watchlist(base(target=None))[0]
    sig = evaluate(3000, p, History(0, None, None, external_low_cents=3100, external_avg_cents=4000, external_source="Keepa"))
    assert {s.rule for s in sig} == {"lowest", "drop"}


def test_config_errors():
    with pytest.raises(ConfigError):
        parse_watchlist({"products": [{"id": "Bad Id", "offers": [A]}]})
    with pytest.raises(ConfigError):
        parse_watchlist({"products": [{"id": "x", "offers": []}]})
    with pytest.raises(ConfigError):
        parse_watchlist({"products": [{"id": "x", "offers": [A], "rules": {"typo": 1}}]})


# --- moteur ----------------------------------------------------------------------------------

def test_best_offer_includes_shipping_and_stock():
    engine, products, fetcher, rec, _ = make(base(target=40))
    fetcher.pages = {A: page("41.00"), B: page("35.00")}  # B = 35 + 5 port = 40 < 41
    report = engine.run_cycle(products)[0]
    assert report.best.offer.url == B and report.best.total_cents == 4000
    assert len(rec.deals()) == 1
    assert "Ludum" in rec.deals()[0].title

    engine, products, fetcher, rec, _ = make(base(target=40))
    fetcher.pages = {A: page("41.00"), B: page("30.00", "OutOfStock")}
    report = engine.run_cycle(products)[0]
    assert report.best.offer.url == A


def test_no_spam_then_further_drop_then_rearm():
    engine, products, fetcher, rec, clock = make(base(target=40))
    fetcher.pages = {A: page("39.00"), B: page("60")}
    engine.run_cycle(products)
    assert len(rec.deals()) == 1

    clock.t += 3 * 3600
    engine.run_cycle(products)  # même prix : silence
    assert len(rec.deals()) == 1

    fetcher.pages[A] = page("38.80")  # -0,5 % : pas assez
    clock.t += 3 * 3600
    engine.run_cycle(products)
    assert len(rec.deals()) == 1

    fetcher.pages[A] = page("36.00")  # -7,7 % : nouvelle alerte
    clock.t += 3 * 3600
    engine.run_cycle(products)
    assert len(rec.deals()) == 2

    fetcher.pages[A] = page("50.00")  # remonte : réarmement après cooldown (12 h)
    clock.t += 13 * 3600
    engine.run_cycle(products)
    fetcher.pages[A] = page("39.00")
    clock.t += 3 * 3600
    engine.run_cycle(products)
    assert len(rec.deals()) == 3


def test_history_drop_detected_after_some_days():
    engine, products, fetcher, rec, clock = make(base(target=None, min_history_days=5))
    fetcher.pages = {A: page("50.00"), B: page("60")}
    for _ in range(6):
        engine.run_cycle(products)
        clock.t += DAY
    assert rec.deals() == []
    fetcher.pages[A] = page("40.00")
    engine.run_cycle(products)
    deal = rec.deals()[0]
    assert "Nouveau plus bas" in deal.message and "-20 % vs médiane" in deal.message


def test_failure_alert_once_and_recovery():
    engine, products, fetcher, rec, clock = make(base(target=10))
    fetcher.pages = {A: FetchError("HTTP 403 (probable anti-bot)", blocked=True), B: page("60")}
    for _ in range(4):
        engine.run_cycle(products)
        clock.t += 3600
    system = [n for n in rec.sent if n.kind == "system"]
    assert len(system) == 1 and "Philibert" in system[0].title

    fetcher.pages[A] = page("50")
    engine.run_cycle(products)
    system = [n for n in rec.sent if n.kind == "system"]
    assert len(system) == 2 and "rétabli" in system[1].message


def test_price_not_found_is_failure():
    engine, products, fetcher, rec, _ = make(base(target=10))
    fetcher.pages = {A: "<html>rien</html>", B: page("60")}
    report = engine.run_cycle(products)[0]
    assert report.results[0].error == "prix introuvable dans la page"


# --- frais de port ---------------------------------------------------------------------------

def test_shipping_resolution_order():
    data = {
        "stores": {"www.philibertnet.com": {"cost": "4.90", "free_above": 60}},
        "products": [{"id": "x", "offers": [A, {"url": A + "?v=2", "shipping": 0}, B]}],
    }
    a, forced, b = parse_watchlist(data)[0].offers
    assert a.shipping_for(5000, None) == (490, "boutique")
    assert a.shipping_for(5000, 300) == (300, "page")  # la page prime sur la politique boutique
    assert a.shipping_for(6000, 300) == (0, "offert")  # seuil atteint
    assert forced.shipping_for(5000, 300) == (0, "config")
    assert b.shipping_for(5000, None) == (None, "inconnu")


def test_shipping_changes_best_offer_and_message():
    data = {
        "stores": {"ludum.fr": {"cost": 6, "free_above": 40}},
        "products": [{"id": "jeu", "name": "Jeu", "target_price": 45, "offers": [A, B]}],
    }
    engine, products, fetcher, rec, _ = make(data)
    fetcher.pages = {A: page("39.00"), B: page("39.50")}  # Philibert : port inconnu ; Ludum : 39,50 + 6
    report = engine.run_cycle(products)[0]
    assert report.best.offer.url == A
    msg = rec.deals()[0].message
    assert "frais de port inconnus" in msg and "Ludum : 45,50 €" in msg and "dont 6,00 € de port" in msg

    fetcher.pages[B] = page("40.00")  # seuil atteint : port offert → 40 €, mais Philibert reste à 39
    assert engine.run_cycle(products)[0].results[1].shipping_source == "offert"


def test_default_shipping_estimate():
    data = {"default_shipping": 4.90, "stores": {"ludum.fr": {"free_above": 40}},
            "products": [{"id": "x", "offers": [B]}]}
    offer = parse_watchlist(data)[0].offers[0]
    assert offer.shipping_for(3000, None) == (490, "estimé")
    assert offer.shipping_for(4000, None) == (0, "offert")
    assert offer.shipping_for(3000, 250) == (250, "page")


def test_shipping_tiers():
    data = {"stores": {"ludum.fr": {"free_above": 49, "cost": 9,
                                     "tiers": [{"below": 30, "cost": 3.20}, {"below": 15, "cost": 3.90}]}},
            "products": [{"id": "x", "offers": [B]}]}
    offer = parse_watchlist(data)[0].offers[0]
    assert offer.shipping_for(1000, None) == (390, "boutique")
    assert offer.shipping_for(2000, None) == (320, "boutique")
    assert offer.shipping_for(4000, None) == (900, "boutique")
    assert offer.shipping_for(4900, None) == (0, "offert")


def test_store_policy_config_errors():
    with pytest.raises(ConfigError):
        parse_watchlist({"stores": {"ludum.fr": {"price": 3}}, "products": []})
    with pytest.raises(ConfigError):
        parse_watchlist({"products": [{"id": "x", "offers": [{"url": A, "shiping": 3}]}]})


# --- ntfy ------------------------------------------------------------------------------------

def test_ntfy_payload_and_auth():
    captured = {}

    def handler(request: httpx.Request):
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "x"})

    n = NtfyNotifier("https://ntfy.lan/", "deals", system_topic="dealarr-sys", token="tk_placeholder",
                     client=httpx.Client(transport=httpx.MockTransport(handler)))
    n.send(Notification("T", "M", priority=9, tags=("moneybag",), url=A, actions=(("Voir", A),) * 5))
    assert captured["url"] == "https://ntfy.lan"
    assert captured["auth"] == "Bearer tk_placeholder"
    body = captured["body"]
    assert body["topic"] == "deals" and body["priority"] == 5 and body["click"] == A
    assert len(body["actions"]) == 3 and body["actions"][0]["action"] == "view"

    n.send(Notification("T", "M", kind="system"))
    assert captured["body"]["topic"] == "dealarr-sys"


def test_ntfy_error_raises():
    from dealarr.notify import NotifyError

    n = NtfyNotifier("https://ntfy.lan", "deals",
                     client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="forbidden"))))
    with pytest.raises(NotifyError):
        n.send(Notification("T", "M"))


def test_ntfy_from_env_requires_url_and_topic():
    with pytest.raises(ValueError):
        NtfyNotifier.from_env({"NTFY_URL": "https://ntfy.lan"})
