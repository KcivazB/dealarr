"""Recherche multi-boutiques : un nom (ou un code-barres, ou une URL) → candidats regroupés par produit.

Pour chaque boutique compatible : page de recherche du site → liens produit → lecture de chaque fiche
(prix, port, stock, EAN). Les fiches partageant un même EAN sont regroupées automatiquement ; sans EAN,
regroupement par titre, marqué « à vérifier ».
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from urllib.parse import quote_plus, urljoin

from html import unescape as html_unescape

from .config import Offer
from .extract import extract, looks_blocked, normalize_ean
from .fetch import FetchError, Fetcher
from .sites import canonical_url, profile_for

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoreSearch:
    domain: str
    name: str
    search_url: str  # {q} = requête encodée
    product_link: str  # regex des URLs de fiches produit
    browser: bool = False  # page rendue en JavaScript (jamais pour franchir un anti-bot)


# Vérifié le 05/11/2026 (HTTP simple, sans navigateur).
SEARCHABLE: tuple[StoreSearch, ...] = (
    StoreSearch("philibertnet.com", "Philibert", "https://www.philibertnet.com/fr/recherche?search_query={q}",
                r"https://www\.philibertnet\.com/fr/[a-z0-9-]+/\d+-[a-z0-9-]+\.html"),
    StoreSearch("ludum.fr", "Ludum", "https://www.ludum.fr/module/pkelements/search?s={q}",
                r"https://www\.ludum\.fr/[a-z0-9-]+/[a-z0-9-]+-\d+"),
    StoreSearch("bcd-jeux.fr", "BCD Jeux", "https://www.bcd-jeux.fr/recherche?controller=search&search_query={q}",
                r"https://www\.bcd-jeux\.fr/[a-z0-9-]+/\d+-[a-z0-9-]+\.html"),
    StoreSearch("le-passe-temps.com", "Le Passe Temps", "https://www.le-passe-temps.com/recherche?controller=search&s={q}",
                r"https://www\.le-passe-temps\.com/[a-z0-9-]+/\d+-[a-z0-9-]+\.html"),
    StoreSearch("lepion.com", "Le Pion Magique", "https://www.lepion.com/?s={q}&post_type=product",
                r"https://www\.lepion\.com/(?:jeu/[a-z0-9-]+/|[a-z0-9-]+/\d+-[a-z0-9-]+\.html)"),
    StoreSearch("espritjeu.com", "Esprit Jeu", "https://www.espritjeu.com/dhtml/resultat_recherche.php?keywords={q}",
                r"https://www\.espritjeu\.com/(?:[a-z0-9-]+/)?[a-z0-9-]+\.html"),
    StoreSearch("ipa-gameshop.com", "IPA Gameshop", "https://ipa-gameshop.com/fr/recherche?controller=search&s={q}",
                r"https://ipa-gameshop\.com/fr/[a-z0-9-]+/\d+-\d+-[a-z0-9-]+\.html"),
    StoreSearch("lagranderecre.fr", "La Grande Récré",
                "https://www.lagranderecre.fr/la-grande-recre/resultat-d-une-recherche-produits.html?searchText={q}",
                r"https://www\.lagranderecre\.fr/(?!la-grande-recre/)[a-z0-9/-]+\.html"),
    StoreSearch("e.leclerc", "E.Leclerc", "https://www.e.leclerc/recherche?q={q}",
                r"https://www\.e\.leclerc/fp/[a-z0-9-]+"),
)

_BY_DOMAIN = {s.domain: s for s in SEARCHABLE}

# LEGO : recherche par numéro de set (vérifié le 05/11/2026, HTTP simple).
# lego.com est protégé par Cloudflare (anti-bot) : non interrogé, lien manuel uniquement.
LEGO_SEARCHABLE: tuple[StoreSearch, ...] = (
    StoreSearch("e.leclerc", "E.Leclerc", "https://www.e.leclerc/recherche?q=lego+{q}",
                _BY_DOMAIN["e.leclerc"].product_link),
    _BY_DOMAIN["lagranderecre.fr"],
)
LEGO_MANUAL: tuple[tuple[str, str], ...] = (
    ("LEGO.com", "https://www.lego.com/fr-fr/search?q={q}"),
    ("King Jouet", "https://www.king-jouet.com/"),
    ("Cultura", "https://www.cultura.com/search/results?search_query=lego+{q}"),
    ("Amazon", "https://www.amazon.fr/s?k=lego+{q}"),
)
_SET_NUMBER = re.compile(r"\d{4,6}")


def is_set_number(query: str) -> bool:
    """Numéro de set LEGO : 4 à 6 chiffres (un EAN en compte 8 à 14)."""
    return bool(_SET_NUMBER.fullmatch(query.strip()))


def _has_set_number(text: str | None, number: str) -> bool:
    return bool(text) and re.search(rf"(?<!\d){number}(?!\d)", text) is not None


def _is_lego_set(url: str, title: str | None, number: str) -> bool:
    text = f"{url} {title or ''}"
    return "lego" in text.lower() and _has_set_number(text, number)


# Recherche automatique impossible (JS + anti-bot) : lien pour chercher à la main.
MANUAL: tuple[tuple[str, str], ...] = (
    ("King Jouet", "https://www.king-jouet.com/"),
    ("Cultura", "https://www.cultura.com/search/results?search_query={q}"),
    ("Amazon", "https://www.amazon.fr/s?k={q}"),
)

MAX_PER_STORE = 4
_STOPWORDS = {"le", "la", "les", "de", "des", "du", "un", "une", "et", "jeu", "the", "of", "a", "l", "d"}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _tokens(query: str) -> list[str]:
    return [t for t in _norm(query).split() if t not in _STOPWORDS and len(t) >= 2]


@dataclass
class Candidate:
    store: str
    url: str
    title: str | None = None
    price_cents: int | None = None
    shipping_cents: int | None = None
    shipping_source: str = "inconnu"
    in_stock: bool | None = None
    ean: str | None = None
    error: str | None = None

    @property
    def total_cents(self) -> int | None:
        return None if self.price_cents is None else self.price_cents + (self.shipping_cents or 0)


@dataclass
class Group:
    key: str
    title: str
    ean: str | None
    candidates: list[Candidate] = field(default_factory=list)
    fuzzy: bool = False  # regroupé par titre (pas d'EAN commun)
    relevance: float = 0.0

    @property
    def best(self) -> Candidate | None:
        priced = [c for c in self.candidates if c.total_cents is not None and c.in_stock is not False]
        return min(priced, key=lambda c: c.total_cents, default=None)


@dataclass
class SearchResult:
    query: str
    groups: list[Group]
    failures: list[tuple[str, str]]  # (boutique, erreur)
    manual: list[tuple[str, str]]
    seconds: float
    target_ean: str | None = None
    set_number: str | None = None


def _store_links(fetcher: Fetcher, store: StoreSearch, query: str, strict: bool,
                 set_number: str | None = None) -> list[str]:
    html = fetcher.get(store.search_url.format(q=quote_plus(query)), browser=store.browser)
    text = html.replace("\\/", "/")
    # Liens href (relatifs ou absolus) et URLs brutes (JSON, ex. Ludum), dans l'ordre de la page.
    links: list[str] = []
    for m in re.finditer(r"""href=["']([^"'#\\]+)|(https?://[^\s"'<>\\]+)""", text):
        url = urljoin(store.search_url, html_unescape(m.group(1) or m.group(2)))
        url = url.split("#")[0].split("?")[0]
        if re.fullmatch(store.product_link, url) and url not in links:
            links.append(url)
    if set_number is not None:
        return [u for u in links if _has_set_number(u, set_number)][:MAX_PER_STORE]
    if not strict:
        return links[:MAX_PER_STORE]
    tokens = _tokens(query)
    scored = []
    for i, url in enumerate(links):
        slug = _norm(url.split("://", 1)[-1].split("/", 1)[-1])
        hits = sum(1 for t in tokens if t in slug.split())
        if tokens and hits / len(tokens) >= 0.5:
            scored.append((-hits, i, url))
    return [u for _, _, u in sorted(scored)[:MAX_PER_STORE]]


def _search_store(store: StoreSearch, query: str, strict: bool,
                  offer_for, browser_enabled: bool, set_number: str | None = None) -> tuple[list[Candidate], str | None]:
    fetcher = Fetcher((0.4, 1.0), browser_enabled)
    try:
        try:
            links = _store_links(fetcher, store, query, strict=strict, set_number=set_number)
        except FetchError as exc:
            return [], str(exc)
        candidates = []
        for url in links:
            url = canonical_url(url)
            cand = Candidate(store=store.name, url=url)
            try:
                profile = profile_for(url)
                html = fetcher.get(url, browser=store.browser or bool(profile and profile.needs_browser))
                ex = extract(html, profile, url=url)
                if not ex.ok:
                    raise FetchError("page anti-bot" if looks_blocked(html) else "prix introuvable")
                offer: Offer = offer_for(url)
                cand.store = offer.store
                cand.title, cand.price_cents, cand.in_stock, cand.ean = ex.title, ex.price_cents, ex.in_stock, ex.ean
                cand.shipping_cents, cand.shipping_source = offer.shipping_for(ex.price_cents, ex.shipping_cents)
            except (FetchError, Exception) as exc:  # une fiche en erreur n'arrête pas la boutique
                cand.error = str(exc) if isinstance(exc, FetchError) else exc.__class__.__name__
            if set_number is None or _is_lego_set(url, cand.title, set_number):
                candidates.append(cand)
        return candidates, None
    finally:
        fetcher.close()


def _group(candidates: list[Candidate], query: str) -> list[Group]:
    groups: dict[str, Group] = {}
    loose: list[Candidate] = []
    for c in candidates:
        if c.error or c.price_cents is None:
            continue
        if c.ean:
            g = groups.setdefault(c.ean, Group(key=c.ean, title=c.title or "", ean=c.ean))
            g.candidates.append(c)
            if c.title and (not g.title or len(c.title) < len(g.title)):
                g.title = c.title  # le titre le plus court est en général le plus propre
        else:
            loose.append(c)
    for c in loose:  # sans EAN : rattacher au groupe au titre le plus proche, sinon nouveau groupe
        title = _norm(c.title or "")
        best, score = None, 0.0
        for g in groups.values():
            s = SequenceMatcher(None, title, _norm(g.title)).ratio()
            if s > score:
                best, score = g, s
        if best is not None and score >= 0.92:
            best.candidates.append(c)
            best.fuzzy = True
        else:
            key = f"t:{title}"
            g = groups.setdefault(key, Group(key=key, title=c.title or c.url, ean=None, fuzzy=True))
            g.candidates.append(c)

    q = _norm(query)
    for g in groups.values():
        g.relevance = SequenceMatcher(None, q, _norm(g.title)).ratio()
        stores = {}
        for c in sorted(g.candidates, key=lambda c: (c.total_cents is None, c.total_cents or 0)):
            stores.setdefault(c.store, c)  # une seule fiche par boutique (la moins chère)
        g.candidates = list(stores.values())
    return sorted(groups.values(), key=lambda g: (-round(g.relevance, 1), -len(g.candidates), g.title))


def _search_lego(number: str, offer_for, browser_enabled: bool, started: float) -> SearchResult:
    with ThreadPoolExecutor(max_workers=len(LEGO_SEARCHABLE)) as pool:
        futures = {s: pool.submit(_search_store, s, number, False, offer_for, browser_enabled, number)
                   for s in LEGO_SEARCHABLE}
        candidates, failures = [], []
        for store, fut in futures.items():
            found, error = fut.result()
            candidates.extend(found)
            if error:
                failures.append((store.name, error))
    priced = [c for c in candidates if not c.error and c.price_cents is not None]
    groups = []
    if priced:
        best_by_store: dict[str, Candidate] = {}
        for c in sorted(priced, key=lambda c: c.total_cents):
            best_by_store.setdefault(c.store, c)
        titles = [c.title for c in priced if c.title]
        title = min(titles, key=len) if titles else f"LEGO {number}"
        if number not in title:
            title = f"LEGO {number} {title}"
        ean = next((c.ean for c in priced if c.ean), None)
        groups.append(Group(key=f"set:{number}", title=title, ean=ean,
                            candidates=list(best_by_store.values()), relevance=1.0))
    manual = [(name, url.format(q=quote_plus(number))) for name, url in LEGO_MANUAL]
    return SearchResult(query=number, groups=groups, failures=failures, manual=manual,
                        seconds=time.monotonic() - started, set_number=number)


def search(query: str, offer_for, browser_enabled: bool = False, ean: str | None = None,
           kind: str = "auto") -> SearchResult:
    """Nom (+ EAN optionnel), code-barres seul ou URL → groupes de candidats.

    Avec un EAN fourni, le nom sert à chercher et l'EAN à confirmer : le groupe correspondant
    passe en tête et est marqué `SearchResult.target_ean`.

    `offer_for(url) -> Offer` applique les politiques de port connues (Catalog.preview_offer).
    - nom : recherche du nom, liens filtrés sur les mots de la requête ;
    - code-barres : recherche du code dans chaque boutique (toutes ne le gèrent pas) ;
    - URL : lecture de la fiche (titre + code-barres) puis recherche du titre ; le groupe portant
      le même code-barres est remonté en tête.
    """
    started = time.monotonic()
    query = query.strip()
    if kind == "lego" or (kind == "auto" and is_set_number(query)):
        number = re.sub(r"\D", "", query)
        if not is_set_number(number):
            raise FetchError("numéro de set LEGO attendu (4 à 6 chiffres, ex. 75313)")
        return _search_lego(number, offer_for, browser_enabled, started)
    target_ean: str | None = None
    seed: Candidate | None = None
    strict = True
    if ean and (target := normalize_ean(ean)):
        target_ean = target

    if query.startswith(("http://", "https://")):
        url = canonical_url(query)
        with Fetcher((0, 0), browser_enabled) as fetcher:
            html = fetcher.get(url)
        ex = extract(html, profile_for(url), url=url)
        if not ex.title and not ex.ean:
            raise FetchError("impossible de lire cette page (ni titre ni code-barres)")
        offer = offer_for(url)
        seed = Candidate(store=offer.store, url=url, title=ex.title, price_cents=ex.price_cents,
                         in_stock=ex.in_stock, ean=ex.ean)
        if ex.price_cents is not None:
            seed.shipping_cents, seed.shipping_source = offer.shipping_for(ex.price_cents, ex.shipping_cents)
        target_ean, query = target_ean or ex.ean, (ex.title or ex.ean)
    elif re.fullmatch(r"\d{8,14}", query.replace(" ", "")):
        target_ean, strict = normalize_ean(query), False
        query = query.replace(" ", "")

    with ThreadPoolExecutor(max_workers=len(SEARCHABLE)) as pool:
        futures = {s: pool.submit(_search_store, s, query, strict, offer_for, browser_enabled) for s in SEARCHABLE}
        candidates, failures = [], []
        for store, fut in futures.items():
            found, error = fut.result()
            candidates.extend(found)
            if error:
                failures.append((store.name, error))

    if strict and target_ean:
        # 2e passe : boutiques sans fiche au bon EAN → recherche directe du code-barres.
        matched = {c.store for c in candidates if c.ean == target_ean}
        retry = [s for s in SEARCHABLE if s.name not in matched and s.name not in dict(failures)]
        with ThreadPoolExecutor(max_workers=max(len(retry), 1)) as pool:
            futures = [pool.submit(_search_store, s, target_ean.zfill(13), False, offer_for, browser_enabled)
                       for s in retry]
            for fut in futures:
                found, _ = fut.result()
                candidates.extend(c for c in found if c.ean == target_ean and c.url not in {x.url for x in candidates})

    if seed is not None and seed.price_cents is not None and not any(c.url == seed.url for c in candidates):
        candidates.append(seed)
    if not strict:  # recherche par code-barres : ne garder que les fiches correspondantes
        candidates = [c for c in candidates if c.ean == target_ean]
    groups = _group(candidates, query)
    if target_ean:
        groups.sort(key=lambda g: g.ean != target_ean)  # tri stable : le bon produit en tête
    manual = [(name, url.format(q=quote_plus(query))) for name, url in MANUAL]
    return SearchResult(query=query, groups=groups, failures=failures, manual=manual,
                        seconds=time.monotonic() - started, target_ean=target_ean)
