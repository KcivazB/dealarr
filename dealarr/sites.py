"""Profils par boutique : nom affiché et, si nécessaire, sélecteurs CSS spécifiques.

La plupart des boutiques exposent le prix en JSON-LD / microdata schema.org : aucun profil
n'est alors nécessaire. On n'ajoute un profil que pour un site vérifié qui en a besoin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class SiteProfile:
    name: str
    price_selectors: tuple[str, ...] = ()
    title_selectors: tuple[str, ...] = ()
    stock_selectors: tuple[str, ...] = ()
    out_of_stock_patterns: tuple[str, ...] = ()
    needs_browser: bool = False
    # True : les sélecteurs du profil passent avant le microdata générique
    # (utile quand la page contient d'autres prix : produits associés, carrousels...).
    prefer_selectors: bool = False
    # Frais de port affichés dans la page (lus dans l'attribut shipping_attr, sinon dans le texte).
    shipping_selectors: tuple[str, ...] = ()
    shipping_attr: str | None = None


_AMAZON = SiteProfile(
    name="Amazon",
    price_selectors=(
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        "#priceblock_dealprice",
        "#priceblock_ourprice",
    ),
    title_selectors=("#productTitle",),
    stock_selectors=("#availability",),
    out_of_stock_patterns=("actuellement indisponible", "currently unavailable"),
    # Livraison principale : attribut "GRATUITE" ou un montant.
    shipping_selectors=("#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE [data-csa-c-delivery-price]",),
    shipping_attr="data-csa-c-delivery-price",
)

# Clé = domaine sans "www." (les sous-domaines correspondent aussi).
PROFILES: dict[str, SiteProfile] = {
    "amazon.fr": _AMAZON,
    "philibertnet.com": SiteProfile("Philibert"),
    "ludum.fr": SiteProfile("Ludum"),
    "bcd-jeux.fr": SiteProfile("BCD Jeux"),
    "lagranderecre.fr": SiteProfile("La Grande Récré"),
    # LEGO.com : protégé par Cloudflare (anti-bot) → non suivi automatiquement.
    "lego.com": SiteProfile("LEGO"),
    "lepion.com": SiteProfile("Le Pion Magique"),
    "espritjeu.com": SiteProfile("Esprit Jeu"),
    # IPA : l'id de variante (langue) est dans le chemin (ex. 2743-4832-...), le JSON-LD le reflète.
    "ipa-gameshop.com": SiteProfile("IPA Gameshop"),
    # Le Passe Temps : pas de JSON-LD ; la page contient aussi les prix des produits associés.
    "le-passe-temps.com": SiteProfile(
        "Le Passe Temps",
        price_selectors=(".nq-c-ProductPrices-current",),
        prefer_selectors=True,
    ),
    # Fnac : protégé par un anti-bot (403 en HTTP et en headless, testé le 05/11/2026).
    "fnac.com": SiteProfile("Fnac"),
    "cultura.com": SiteProfile("Cultura"),
    "king-jouet.com": SiteProfile("King Jouet"),
    "e.leclerc": SiteProfile("E.Leclerc"),
}


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def profile_for(url: str) -> SiteProfile | None:
    host = domain_of(url)
    for key, profile in PROFILES.items():
        if host == key or host.endswith("." + key):
            return profile
    return None


def store_name(url: str) -> str:
    profile = profile_for(url)
    return profile.name if profile else domain_of(url)


_ASIN = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)", re.I)


def amazon_asin(url: str) -> str | None:
    if not domain_of(url).startswith("amazon."):
        return None
    m = _ASIN.search(urlparse(url).path + "/")
    return m.group(1).upper() if m else None


def canonical_url(url: str) -> str:
    """Retire les paramètres de tracking quand on sait le faire (Amazon)."""
    asin = amazon_asin(url)
    if asin:
        return f"https://www.{domain_of(url)}/dp/{asin}"
    return url
