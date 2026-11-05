"""Extraction prix / disponibilité / titre depuis une page produit.

Ordre : sélecteur explicite (watchlist) > JSON-LD schema.org > microdata / meta > profil du site.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, replace
from typing import Iterator

from bs4 import BeautifulSoup

from .money import parse_price
from .sites import SiteProfile

_IN_STOCK = ("instock", "limitedavailability", "onlineonly", "preorder", "presale", "backorder")
_OUT_OF_STOCK = ("outofstock", "soldout", "discontinued", "instoreonly")

# Marqueurs de pages anti-bot (utilisés uniquement si aucun prix n'a été trouvé).
_BLOCK_MARKERS = (
    "captcha-delivery.com",  # DataDome
    "validatecaptcha",  # Amazon
    "cf-chl-",  # Cloudflare challenge
    "px-captcha",  # PerimeterX
    "/_incapsula_resource",
    "please enable js and disable any ad blocker",
)


@dataclass(frozen=True)
class Extraction:
    price_cents: int | None
    in_stock: bool | None
    title: str | None
    method: str
    currency: str | None = None
    shipping_cents: int | None = None  # frais de port lus dans la page
    ean: str | None = None  # code-barres (GTIN/EAN), normalisé sans zéros de tête

    @property
    def ok(self) -> bool:
        return self.price_cents is not None


def extract(html: str, profile: SiteProfile | None = None, selector: str | None = None,
            url: str | None = None) -> Extraction:
    soup = BeautifulSoup(html, "lxml")
    result = _extract_price(soup, profile, selector)
    if result.ok:
        shipping = _jsonld_shipping(soup)
        if shipping is None and profile and profile.shipping_selectors:
            shipping = _shipping_from_selectors(soup, profile)
        result = replace(result, shipping_cents=shipping)
    return replace(result, ean=_ean(soup, url))


# --- Code-barres -----------------------------------------------------------------------------

_GTIN_KEYS = ("gtin13", "gtin", "gtin12", "gtin14", "gtin8", "ean", "isbn")
_EMBEDDED_EAN = re.compile(r'"(?:ean13|ean|gtin13|gtin)"\s*:\s*"(\d{8,14})"', re.I)
_URL_EAN = re.compile(r"(?<!\d)(\d{12,14})(?:\.html?|/)?(?:[?#]|$)")


def normalize_ean(value: object) -> str | None:
    """GTIN-8/12/13/14 → chiffres sans zéros de tête (UPC 826956620105 == EAN 0826956620105)."""
    digits = re.sub(r"\D", "", str(value or ""))
    if not 8 <= len(digits) <= 14:
        return None
    return digits.lstrip("0") or None


def _ean(soup: BeautifulSoup, url: str | None) -> str | None:
    for node in _iter_jsonld(soup):
        if not _types(node) & {"product", "productgroup", "individualproduct"}:
            continue
        offers = node.get("offers")
        for holder in [node, *(offers if isinstance(offers, list) else [offers])]:
            if isinstance(holder, dict):
                for key in _GTIN_KEYS:
                    if (ean := normalize_ean(holder.get(key))):
                        return ean
    el = soup.select_one('[itemprop^="gtin"], meta[property="product:ean"], meta[property="og:upc"]')
    if el is not None and (ean := normalize_ean(el.get("content") or el.get_text(strip=True))):
        return ean
    raw = html.unescape(str(soup))[:600_000]
    if (m := _EMBEDDED_EAN.search(raw)):  # JSON embarqué (PrestaShop, Magento…)
        return normalize_ean(m.group(1))
    if url and (m := _URL_EAN.search(url.split("?")[0])):
        return normalize_ean(m.group(1))
    return None


def _extract_price(soup: BeautifulSoup, profile: SiteProfile | None, selector: str | None) -> Extraction:
    title = _title(soup, profile)

    if selector:
        price = _price_from_selectors(soup, (selector,))
        stock = _stock_from_profile(soup, profile) if profile else _stock_from_microdata(soup)
        if price is not None:
            return Extraction(price, stock, title, "selector")

    if profile and profile.prefer_selectors:
        price = _price_from_selectors(soup, profile.price_selectors)
        if price is not None:
            return Extraction(price, _stock_from_profile(soup, profile), title, "profile")

    jl = _from_jsonld(soup)
    if jl is not None and jl.ok:
        return Extraction(jl.price_cents, jl.in_stock, jl.title or title, "jsonld", jl.currency)

    md = _from_microdata(soup)
    if md is not None and md.ok:
        return Extraction(md.price_cents, md.in_stock, title, "microdata", md.currency)

    if profile and profile.price_selectors:
        price = _price_from_selectors(soup, profile.price_selectors)
        if price is not None:
            return Extraction(price, _stock_from_profile(soup, profile), title, "profile")

    return Extraction(None, None, title, "none")


# --- Frais de port ---------------------------------------------------------------------------


def _amount_or_zero(value: object) -> int | None:
    """Comme parse_price, mais accepte 0 (livraison gratuite) et les mentions 'gratuit'."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if "gratuit" in text or "offert" in text or "free" in text:
        return 0
    if re.fullmatch(r"0+([.,]0+)?\s*€?", text):
        return 0
    return parse_price(value)


def _jsonld_shipping(soup: BeautifulSoup) -> int | None:
    """Plus petit tarif schema.org OfferShippingDetails à destination de la France (ou sans pays)."""
    rates = []
    for node in _iter_jsonld(soup):
        if not _types(node) & {"product", "productgroup", "individualproduct"}:
            continue
        offers = node.get("offers")
        for offer in offers if isinstance(offers, list) else [offers]:
            if not isinstance(offer, dict):
                continue
            details = offer.get("shippingDetails")
            for d in details if isinstance(details, list) else [details]:
                if not isinstance(d, dict):
                    continue
                dest = d.get("shippingDestination")
                countries = {
                    str(x.get("addressCountry", "")).upper()
                    for x in (dest if isinstance(dest, list) else [dest]) if isinstance(x, dict)
                } - {""}
                if countries and "FR" not in countries:
                    continue
                rate = d.get("shippingRate")
                if isinstance(rate, dict) and str(rate.get("currency") or "EUR").upper() == "EUR":
                    if (amount := _amount_or_zero(rate.get("value"))) is not None:
                        rates.append(amount)
    return min(rates) if rates else None


def _shipping_from_selectors(soup: BeautifulSoup, profile: SiteProfile) -> int | None:
    for sel in profile.shipping_selectors:
        el = soup.select_one(sel)
        if el is None:
            continue
        raw = el.get(profile.shipping_attr) if profile.shipping_attr else el.get_text(" ", strip=True)
        if raw and len(raw) <= 40:  # évite de parser une phrase contenant une date
            return _amount_or_zero(raw)
    return None


def looks_blocked(html: str) -> bool:
    lower = html[:200_000].lower()
    return any(marker in lower for marker in _BLOCK_MARKERS)


# --- JSON-LD -----------------------------------------------------------------------------


def _iter_jsonld(soup: BeautifulSoup) -> Iterator[dict]:
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        text = tag.string or tag.get_text() or ""
        try:
            data = json.loads(text, strict=False)
        except (ValueError, TypeError):
            continue
        yield from _walk(data)


def _walk(node: object) -> Iterator[dict]:
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "hasVariant"):
            if key in node:
                yield from _walk(node[key])


def _types(node: dict) -> set[str]:
    t = node.get("@type", ())
    return {str(x).lower() for x in (t if isinstance(t, list) else [t])}


def _from_jsonld(soup: BeautifulSoup) -> Extraction | None:
    best: tuple[int, bool | None, str | None] | None = None
    title = None
    for node in _iter_jsonld(soup):
        if not _types(node) & {"product", "productgroup", "individualproduct"}:
            continue
        title = title or _clean(node.get("name"))
        for price, stock, currency in _offers(node.get("offers")):
            if currency and currency.upper() != "EUR":
                continue
            # On garde le prix le plus bas parmi les offres disponibles,
            # sinon le plus bas tout court.
            candidate = (price, stock, currency)
            if best is None or _better(candidate, best):
                best = candidate
    if best is None:
        return None
    return Extraction(best[0], best[1], title, "jsonld", best[2])


def _better(a: tuple, b: tuple) -> bool:
    a_avail, b_avail = a[1] is not False, b[1] is not False
    if a_avail != b_avail:
        return a_avail
    return a[0] < b[0]


def _offers(offers: object) -> Iterator[tuple[int, bool | None, str | None]]:
    if isinstance(offers, list):
        for o in offers:
            yield from _offers(o)
        return
    if not isinstance(offers, dict):
        return
    if "offers" in offers and "aggregateoffer" in _types(offers):
        yield from _offers(offers["offers"])
    stock = _availability(offers.get("availability"))
    currency = offers.get("priceCurrency")
    price = parse_price(offers.get("price"))
    if price is None:
        price = parse_price(offers.get("lowPrice"))
    if price is None:
        spec = offers.get("priceSpecification")
        specs = spec if isinstance(spec, list) else [spec]
        for s in specs:
            if isinstance(s, dict) and (p := parse_price(s.get("price"))) is not None:
                price, currency = p, currency or s.get("priceCurrency")
                break
    if price is not None:
        yield price, stock, str(currency) if currency else None


def _availability(value: object) -> bool | None:
    if not value:
        return None
    v = str(value).lower().rsplit("/", 1)[-1].replace("_", "").replace(" ", "")
    if any(k in v for k in _OUT_OF_STOCK):
        return False
    if any(k in v for k in _IN_STOCK):
        return True
    return None


# --- Microdata / meta ----------------------------------------------------------------------


def _from_microdata(soup: BeautifulSoup) -> Extraction | None:
    price = None
    for sel in (
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
        '[itemprop="price"]',
        '[itemprop="lowPrice"]',
    ):
        for el in soup.select(sel):
            price = parse_price(el.get("content") or el.get_text(" ", strip=True))
            if price is not None:
                break
        if price is not None:
            break
    if price is None:
        return None
    currency_el = soup.select_one('meta[property="product:price:currency"], [itemprop="priceCurrency"]')
    currency = (currency_el.get("content") or currency_el.get_text(strip=True)) if currency_el else None
    if currency and currency.upper() != "EUR":
        return None
    return Extraction(price, _stock_from_microdata(soup), None, "microdata", currency)


def _stock_from_microdata(soup: BeautifulSoup) -> bool | None:
    el = soup.select_one('[itemprop="availability"], meta[property="product:availability"], meta[property="og:availability"]')
    if el is None:
        return None
    return _availability(el.get("href") or el.get("content") or el.get_text(strip=True))


# --- Profil / sélecteurs ---------------------------------------------------------------------


def _price_from_selectors(soup: BeautifulSoup, selectors: tuple[str, ...]) -> int | None:
    for sel in selectors:
        for el in soup.select(sel):
            price = parse_price(el.get("content") or el.get_text(" ", strip=True))
            if price is not None:
                return price
    return None


def _stock_from_profile(soup: BeautifulSoup, profile: SiteProfile | None) -> bool | None:
    if profile is None:
        return None
    for sel in profile.stock_selectors:
        el = soup.select_one(sel)
        if el is not None:
            text = el.get_text(" ", strip=True).lower()
            if any(p in text for p in profile.out_of_stock_patterns):
                return False
            return True
    return _stock_from_microdata(soup)


def _title(soup: BeautifulSoup, profile: SiteProfile | None) -> str | None:
    for sel in (profile.title_selectors if profile else ()):
        el = soup.select_one(sel)
        if el and (t := _clean(el.get_text(" ", strip=True))):
            return t
    h1 = soup.find("h1")
    if h1 and (t := _clean(h1.get_text(" ", strip=True))) and len(t) <= 150:
        return t
    og = soup.select_one('meta[property="og:title"]')
    if og and (t := _clean(og.get("content"))):
        return t
    return _clean(soup.title.get_text()) if soup.title else None


def _clean(value: object) -> str | None:
    if not value:
        return None
    text = re.sub(r"\s+", " ", html.unescape(str(value))).strip()
    return text[:200] or None
