"""Graphiques SVG générés côté serveur (aucune dépendance JS ; survol géré par static/app.js).

Une seule série par graphique (meilleur prix du jour, port compris) : pas de légende, le titre
la nomme. Couleurs via classes CSS → variables du thème (clair/sombre).
"""

from __future__ import annotations

from datetime import date

from markupsafe import Markup, escape

from ..money import fmt


def _short(day: str) -> str:
    d = date.fromisoformat(day)
    return d.strftime("%d/%m")


def _nice_step(span: int) -> int:
    for step in (50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 50000):
        if span / step <= 4:
            return step
    return 100000


def sparkline(points: list[tuple[str, int]], width: int = 120, height: int = 32) -> Markup:
    if not points:
        return Markup('<span class="spark-empty">—</span>')
    values = [p for _, p in points]
    lo, hi = min(values), max(values)
    span = max(hi - lo, 1)
    pad = 4
    n = len(points)

    def xy(i: int, v: int) -> tuple[float, float]:
        x = pad + (width - 2 * pad) * (i / (n - 1) if n > 1 else 1)
        y = pad + (height - 2 * pad) * (1 - (v - lo) / span) if hi != lo else height / 2
        return round(x, 1), round(y, 1)

    coords = [xy(i, v) for i, v in enumerate(values)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x},{y}" for i, (x, y) in enumerate(coords))
    lx, ly = coords[-1]
    label = escape(f"Meilleur prix sur {n} jours : de {fmt(lo)} à {fmt(hi)}, dernier {fmt(values[-1])}")
    return Markup(
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{label}">'
        f'<path class="series" d="{path}"/><circle class="dot" cx="{lx}" cy="{ly}" r="3"/></svg>'
    )


def price_chart(points: list[tuple[str, int]], target: int | None = None,
                width: int = 720, height: int = 240) -> Markup:
    if not points:
        return Markup('<p class="muted">Pas encore d\'historique : il apparaîtra après les premières vérifications.</p>')

    values = [p for _, p in points]
    lo_v, hi_v = min(values + ([target] if target else [])), max(values + ([target] if target else []))
    step = _nice_step(max(hi_v - lo_v, 100))
    lo = (lo_v // step) * step
    hi = -(-hi_v // step) * step
    if hi == lo:
        hi = lo + step
    left, right, top, bottom = 64, 76, 16, 28
    pw, ph = width - left - right, height - top - bottom
    days = [date.fromisoformat(d).toordinal() for d, _ in points]
    d0, d1 = days[0], days[-1]
    dspan = max(d1 - d0, 1)

    def x_of(day: int) -> float:
        return round(left + pw * ((day - d0) / dspan if d1 != d0 else 1), 1)

    def y_of(v: int) -> float:
        return round(top + ph * (1 - (v - lo) / (hi - lo)), 1)

    parts: list[str] = []
    tick = lo
    while tick <= hi:
        y = y_of(tick)
        parts.append(f'<line class="grid" x1="{left}" x2="{left + pw}" y1="{y}" y2="{y}"/>')
        parts.append(f'<text class="tick" x="{left - 8}" y="{y + 4}" text-anchor="end">{escape(fmt(tick))}</text>')
        tick += step
    base_y = y_of(lo)
    parts.append(f'<line class="axis" x1="{left}" x2="{left + pw}" y1="{base_y}" y2="{base_y}"/>')
    parts.append(f'<text class="tick" x="{left}" y="{height - 8}">{_short(points[0][0])}</text>')
    if len(points) > 1:
        parts.append(f'<text class="tick" x="{left + pw}" y="{height - 8}" text-anchor="end">{_short(points[-1][0])}</text>')

    if target:
        ty = y_of(target)
        parts.append(f'<line class="target" x1="{left}" x2="{left + pw}" y1="{ty}" y2="{ty}"/>')
        parts.append(f'<text class="target-label" x="{left + pw + 6}" y="{ty + 4}">seuil</text>')

    coords = [(x_of(d), y_of(v)) for d, v in zip(days, values)]
    # Prix en marches : le prix d'un jour tient jusqu'au relevé suivant.
    path = [f"M{coords[0][0]},{coords[0][1]}"]
    for (x, y), (px, py) in zip(coords[1:], coords[:-1]):
        path.append(f"H{x}V{y}")
    parts.append(f'<path class="series" d="{"".join(path)}"/>')
    lx, ly = coords[-1]
    parts.append(f'<circle class="dot ring" cx="{lx}" cy="{ly}" r="4.5"/>')
    parts.append(f'<text class="value-label" x="{lx + 8}" y="{ly + 4}">{escape(fmt(values[-1]))}</text>')

    # Couche de survol : une bande verticale par jour.
    for i, ((day, value), (x, y)) in enumerate(zip(points, coords)):
        x0 = left if i == 0 else (coords[i - 1][0] + x) / 2
        x1 = left + pw if i == len(coords) - 1 else (x + coords[i + 1][0]) / 2
        tip = escape(f"{_short(day)} · {fmt(value)}")
        parts.append(f'<rect class="hit" x="{x0}" y="{top}" width="{max(x1 - x0, 1)}" height="{ph}" '
                     f'data-x="{x}" data-y="{y}" data-tip="{tip}"/>')
    parts.append(f'<line class="xhair" x1="0" x2="0" y1="{top}" y2="{top + ph}"/>')
    parts.append('<circle class="xdot" cx="0" cy="0" r="4"/>')

    label = escape(f"Meilleur prix par jour du {_short(points[0][0])} au {_short(points[-1][0])} : "
                   f"min {fmt(min(values))}, max {fmt(max(values))}, actuel {fmt(values[-1])}")
    return Markup(
        f'<div class="chart"><svg viewBox="0 0 {width} {height}" role="img" aria-label="{label}">{"".join(parts)}</svg>'
        f'<div class="tooltip" hidden></div></div>'
    )
