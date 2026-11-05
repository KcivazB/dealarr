"""Parsing et formatage des prix. Tous les montants internes sont en centimes (int)."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# "29€99" / "29 € 99" (format fréquent sur Amazon, Leclerc...)
_EURO_SPLIT = re.compile(r"(\d[\d\s  .]*)\s*€\s*(\d{2})(?!\d)")
_NUMBER = re.compile(r"\d[\d\s  .,']*")
_SPACES = re.compile(r"[\s  ']")


def parse_price(value: object) -> int | None:
    """Convertit une valeur de prix (str, int, float) en centimes. None si illisible ou <= 0."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return _to_cents(str(value))

    text = str(value).strip()
    if not text:
        return None

    split = _EURO_SPLIT.search(text)
    if split:
        euros = _SPACES.sub("", split.group(1)).replace(".", "")
        return _to_cents(f"{euros}.{split.group(2)}")

    match = _NUMBER.search(text)
    if not match:
        return None
    raw = _SPACES.sub("", match.group(0)).rstrip(".,")

    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):  # 1.234,56
            raw = raw.replace(".", "").replace(",", ".")
        else:  # 1,234.56
            raw = raw.replace(",", "")
    elif raw.count(",") == 1:  # 29,99 (format FR)
        raw = raw.replace(",", ".")
    elif raw.count(",") > 1:  # 1,234,567
        raw = raw.replace(",", "")
    elif raw.count(".") > 1:  # 1.234.567
        raw = raw.replace(".", "")
    return _to_cents(raw)


def _to_cents(raw: str) -> int | None:
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    return int((amount * 100).quantize(Decimal("1")))


def fmt(cents: int | None) -> str:
    """Formate des centimes en euros à la française : 1 234,56 €."""
    if cents is None:
        return "—"
    euros, rest = divmod(abs(cents), 100)
    sign = "-" if cents < 0 else ""
    return f"{sign}{euros:,}".replace(",", " ") + f",{rest:02d} €"


def pct_diff(value: int, reference: int) -> float:
    """Écart en % de value par rapport à reference (négatif = moins cher)."""
    return (value - reference) / reference * 100
