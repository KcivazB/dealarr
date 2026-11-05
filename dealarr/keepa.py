"""Historique Amazon via l'API Keepa (optionnel, nécessite un abonnement API Keepa).

Utilise l'endpoint documenté `GET https://api.keepa.com/product` avec `stats=90`.
Les prix Keepa sont en centimes ; -1 signifie « pas d'offre ».
Index des séries : 0 = vendu par Amazon, 1 = neuf (tiers).

⚠ Non testé avec une vraie clé : à valider au premier usage (`dealarr probe --keepa <url>`).
La clé n'est jamais loggée (les logs httpx sont coupés en INFO, cf. cli.py).
"""

from __future__ import annotations

import logging
import time

import httpx

from .storage import ExternalStats

log = logging.getLogger(__name__)

KEEPA_DOMAINS = {"amazon.com": 1, "amazon.co.uk": 2, "amazon.de": 3, "amazon.fr": 4, "amazon.it": 8, "amazon.es": 9}
_SERIES = (0, 1)  # Amazon, neuf tiers


def _positive(values: list, idx: int) -> int | None:
    try:
        v = values[idx]
    except (IndexError, TypeError):
        return None
    if isinstance(v, list):  # minInInterval : [keepaTime, prix]
        v = v[-1] if v else None
    return int(v) if isinstance(v, (int, float)) and v > 0 else None


def fetch_stats(api_key: str, asin: str, domain: str = "amazon.fr", client: httpx.Client | None = None) -> ExternalStats | None:
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        resp = client.get(
            "https://api.keepa.com/product",
            params={"key": api_key, "domain": KEEPA_DOMAINS.get(domain, 4), "asin": asin, "stats": 90},
        )
    except httpx.HTTPError as exc:
        log.warning("Keepa injoignable : %s", exc.__class__.__name__)
        return None
    finally:
        if own:
            client.close()
    if resp.status_code != 200:
        log.warning("Keepa HTTP %s pour %s", resp.status_code, asin)
        return None
    products = resp.json().get("products") or []
    stats = (products[0] or {}).get("stats") if products else None
    if not stats:
        return None

    lows = [p for i in _SERIES if (p := _positive(stats.get("minInInterval") or [], i))]
    avgs = [p for i in _SERIES if (p := _positive(stats.get("avg90") or [], i))]
    if not lows and not avgs:
        return None
    return ExternalStats("Keepa", min(lows) if lows else None, min(avgs) if avgs else None, int(time.time()))
