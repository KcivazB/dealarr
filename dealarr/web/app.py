"""Interface web : tableau de bord, ajout/édition d'articles, boutiques, réglages.

Sécurité :
- pas d'authentification intégrée : à exposer UNIQUEMENT derrière un proxy authentifiant
  (Authentik / forward-auth) ou en local ;
- CSRF : jeton aléatoire en cookie SameSite=Strict + champ de formulaire / en-tête, comparés
  à temps constant, et vérification de l'en-tête Origin ;
- CSP stricte (aucun script ni style inline), autoescape Jinja, URLs sortantes filtrées (anti-SSRF).
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import __version__
from ..catalog import Catalog
from ..config import ConfigError, Product, RuleSettings, Settings, money_input
from ..extract import extract, looks_blocked
from ..fetch import FetchError, Fetcher
from ..money import fmt, pct_diff
from ..notify import Notification, dispatch
from ..runner import Runner
from ..search import search as run_search
from ..sites import canonical_url, profile_for
from ..storage import LatestObservation, Store
from .charts import price_chart, sparkline

log = logging.getLogger(__name__)

CSRF_COOKIE = "dealarr_csrf"
MAX_PROBE_URLS = 10
MAX_IMPORT_BYTES = 1_000_000
STALE_SECONDS = 2 * 86400
_HERE = Path(__file__).parent
_RULE_TYPES = {f.name: f.type for f in fields(RuleSettings)}
RULE_LABELS = {
    "tolerance_pct": ("Tolérance (%)", "Marge au-dessus du seuil / du plus bas qui déclenche quand même"),
    "drop_pct": ("Baisse vs médiane (%)", "Alerte si le prix passe sous la médiane de ce pourcentage"),
    "lowest_window_days": ("Fenêtre plus bas (j)", "Période du plus bas historique"),
    "median_window_days": ("Fenêtre médiane (j)", "Période de calcul du prix habituel"),
    "min_history_days": ("Historique minimum (j)", "Jours de relevés avant d'activer plus bas / baisse"),
    "require_in_stock": ("Ignorer les ruptures", "Ne compter que les offres en stock"),
    "renotify_drop_pct": ("Re-notifier si -X %", "Nouvelle alerte seulement si le prix baisse encore de X %"),
    "cooldown_hours": ("Réarmement (h)", "Délai avant de réarmer une alerte après remontée du prix"),
}


# --- modèles de vue -------------------------------------------------------------------------------


@dataclass
class Summary:
    product: Product
    latest: dict[str, LatestObservation]
    best: LatestObservation | None
    low: int | None
    median: int | None
    days: int
    series: list[tuple[str, int]]
    alert: tuple[int, int] | None
    failing: int
    pending: bool

    @property
    def target_gap(self) -> float | None:
        if self.best is None or self.product.target_cents is None:
            return None
        return pct_diff(self.best.total_cents, self.product.target_cents)

    @property
    def status(self) -> str:
        """deal | near | watch | nodata | off"""
        if not self.product.enabled:
            return "off"
        if self.best is None:
            return "nodata"
        if self.alert is not None:
            return "deal"
        gap = self.target_gap
        if gap is not None and gap <= 15:
            return "near"
        return "watch"


def summarize(product: Product, store: Store, failures: dict[str, tuple[int, str]],
              runner: Runner | None, series_days: int = 60) -> Summary:
    now = int(time.time())
    urls = {o.url for o in product.offers}
    latest = {o.offer_url: o for o in store.latest_by_offer(product.id) if o.offer_url in urls}
    fresh = [o for o in latest.values() if now - o.ts <= STALE_SECONDS
             and not (product.rules.require_in_stock and o.in_stock is False)]
    best = min(fresh, key=lambda o: o.total_cents, default=None)
    r = product.rules
    days, low, median = store.history_summary(product.id, now + 1, r.lowest_window_days, r.median_window_days,
                                              r.require_in_stock)
    series = store.daily_best(product.id, now - series_days * 86400, now + 1, r.require_in_stock)
    return Summary(
        product=product, latest=latest, best=best, low=low, median=median, days=days, series=series,
        alert=store.get_alert_state(product.id),
        failing=sum(1 for u in urls if failures.get(u, (0, ""))[0] > 0),
        pending=bool(runner and runner.is_pending(product.id)),
    )


# --- utilitaires ----------------------------------------------------------------------------------


def _dt(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M")


def _ago(ts: float | None) -> str:
    if not ts:
        return "jamais"
    delta = int(time.time() - ts)
    if delta < 90:
        return "à l'instant"
    if delta < 3600:
        return f"il y a {delta // 60} min"
    if delta < 86400:
        return f"il y a {delta // 3600} h"
    return f"il y a {delta // 86400} j"


def _go(path: str, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    params = {k: v for k, v in (("ok", ok), ("err", err)) if v}
    return RedirectResponse(path + (("?" + urlencode(params)) if params else ""), status_code=303)


def _check_csrf(request: Request, token: str | None) -> None:
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not token or not secrets.compare_digest(cookie, str(token)):
        raise HTTPException(403, "Jeton CSRF invalide : recharge la page et réessaie.")
    origin = request.headers.get("origin")
    if origin and origin != "null" and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Origine de la requête refusée.")


def _rules_from_form(form, inherit: bool) -> dict:
    """Champs de règles → dict. Vide = hériter (produit) ou valeur par défaut (réglages globaux)."""
    rules = {}
    for name, kind in _RULE_TYPES.items():
        raw = str(form.get(f"rule_{name}", "")).strip().replace(",", ".")
        if raw == "":
            continue
        try:
            if kind == "bool":
                if raw not in ("true", "false"):
                    raise ValueError
                rules[name] = raw == "true"
            elif kind == "int":
                rules[name] = int(float(raw))
            else:
                rules[name] = float(raw)
        except ValueError as exc:
            raise ConfigError(f"{RULE_LABELS[name][0]} : valeur invalide « {raw} »") from exc
        if kind != "bool" and rules[name] < 0:
            raise ConfigError(f"{RULE_LABELS[name][0]} : doit être positif")
    return rules


def _bool_field(form, name: str) -> bool | None:
    value = str(form.get(name, "")).strip()
    return {"true": True, "false": False}.get(value)


def _parse_tiers(text: str) -> list[tuple[int, int]]:
    """« 15:3,90 ; 30:3,20 » → [(1500, 390), (3000, 320)]."""
    tiers = []
    for chunk in text.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ConfigError(f"Palier invalide « {chunk} » (format : seuil:coût)")
        below, cost = chunk.split(":", 1)
        b, c = money_input(below, "palier"), money_input(cost, "palier")
        if b is None or c is None:
            raise ConfigError(f"Palier invalide « {chunk} »")
        tiers.append((b, c))
    return tiers


def probe_urls(settings: Settings, urls: list[str]) -> list[dict]:
    """Aperçu avant ajout : lit chaque page et applique la politique de port connue."""
    store = Store(settings.db_path)
    results = []
    try:
        catalog = Catalog(store.conn)
        known = catalog.products_by_url()
        with Fetcher((0, 0), settings.browser_enabled) as fetcher:
            for raw in urls[:MAX_PROBE_URLS]:
                url = canonical_url(raw.strip())
                item: dict = {"url": url, "ok": False}
                try:
                    offer = catalog.preview_offer(url)
                    item["store"] = offer.store
                    profile = profile_for(url)
                    use_browser = bool(profile and profile.needs_browser)
                    html = fetcher.get(url, browser=use_browser)
                    ex = extract(html, profile)
                    if not ex.ok:
                        raise FetchError("page anti-bot détectée : site non pris en charge" if looks_blocked(html)
                                         else "prix introuvable (un sélecteur CSS sera nécessaire)")
                    shipping, source = offer.shipping_for(ex.price_cents, ex.shipping_cents)
                    item.update(
                        ok=True, title=ex.title, price=fmt(ex.price_cents), price_cents=ex.price_cents,
                        shipping=("inconnu" if shipping is None else "offert" if shipping == 0 else fmt(shipping)),
                        shipping_source=source, total=fmt(ex.price_cents + (shipping or 0)),
                        in_stock={True: "en stock", False: "rupture", None: "stock ?"}[ex.in_stock],
                        method=ex.method,
                    )
                except (FetchError, ConfigError) as exc:
                    item["error"] = str(exc)
                if url in known:
                    item["already"] = known[url]
                results.append(item)
    finally:
        store.close()
    return results


# --- application ----------------------------------------------------------------------------------


def create_app(settings: Settings, runner: Runner | None = None) -> FastAPI:
    app = FastAPI(title="dealarr", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    env = Environment(loader=FileSystemLoader(_HERE / "templates"), autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters.update(eur=fmt, dt=_dt, ago=_ago, pct=lambda v: "—" if v is None else f"{v:+.0f} %")
    env.globals.update(version=__version__, rule_labels=RULE_LABELS, rule_types=_RULE_TYPES,
                       sparkline=sparkline)

    @contextmanager
    def db():
        store = Store(settings.db_path)
        try:
            yield store, Catalog(store.conn)
        finally:
            store.close()

    def render(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
        html = env.get_template(template).render(
            request=request, csrf=request.state.csrf, flash_ok=request.query_params.get("ok"),
            flash_err=request.query_params.get("err"), runner=runner, path=request.url.path, **ctx)
        return HTMLResponse(html, status_code=status_code)

    @app.middleware("http")
    async def security(request: Request, call_next):
        token = request.cookies.get(CSRF_COOKIE)
        new = not token or len(token) < 32
        if new:
            token = secrets.token_urlsafe(32)
        request.state.csrf = token
        response: Response = await call_next(request)
        if new:
            response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="strict",
                                secure=request.url.scheme == "https", path="/")
        response.headers.update({
            "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:;"
                                        " connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
        })
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        if not hasattr(request.state, "csrf"):
            request.state.csrf = ""
        return render(request, "error.html", status_code=exc.status_code, message=exc.detail)

    # --- tableau de bord ----------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with db() as (store, catalog):
            try:
                products = catalog.load_products()
            except ConfigError as exc:
                return render(request, "error.html", message=f"Watchlist invalide : {exc}")
            failures = {u: (c, e) for u, c, e in store.failures()}
            summaries = [summarize(p, store, failures, runner) for p in products]
            order = {"deal": 0, "near": 1, "watch": 2, "nodata": 3, "off": 4}
            summaries.sort(key=lambda s: (order[s.status], s.target_gap if s.target_gap is not None else 999,
                                          s.product.name.lower()))
            last = store.last_check_ts()
        return render(request, "dashboard.html", summaries=summaries, last_check=last,
                      failing=sum(1 for c, _ in failures.values() if c >= settings.failure_threshold))

    @app.post("/check-all")
    async def check_all(request: Request):
        form = await request.form()
        _check_csrf(request, form.get("csrf_token"))
        if runner is None:
            return _go("/", err="Planificateur indisponible dans ce mode")
        with db() as (_, catalog):
            ids = [p.id for p in catalog.load_products() if p.enabled]
        runner.trigger(ids)
        return _go("/", ok="Vérification de tous les articles lancée (quelques minutes)")

    # --- articles ------------------------------------------------------------------------------

    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request):
        q = request.query_params.get("q", "").strip()[:200]
        ean = request.query_params.get("ean", "").strip()[:20]
        kind = request.query_params.get("kind", "auto")
        kind = kind if kind in ("auto", "game", "lego") else "auto"
        if not q:
            return render(request, "search.html", q="", ean=ean, kind=kind, result=None, tracked={})
        with db() as (_, catalog):
            offer_for = catalog.preview_offer
            tracked = catalog.products_by_url()
            try:
                result = run_search(q, offer_for, settings.browser_enabled, ean=ean or None, kind=kind)
            except (FetchError, ConfigError) as exc:
                return render(request, "search.html", q=q, ean=ean, kind=kind, result=None, tracked={}, error=str(exc))
        return render(request, "search.html", q=q, ean=ean, kind=kind, result=result, tracked=tracked)

    @app.get("/products/new", response_class=HTMLResponse)
    def product_new(request: Request):
        return render(request, "product_new.html", prefill_url=request.query_params.get("url", ""))

    @app.post("/api/probe")
    async def api_probe(request: Request):
        _check_csrf(request, request.headers.get("x-csrf-token"))
        body = await request.json()
        urls = [str(u) for u in (body.get("urls") or []) if str(u).strip()]
        if not urls:
            return JSONResponse({"error": "Aucune URL"}, status_code=400)
        if len(urls) > MAX_PROBE_URLS:
            return JSONResponse({"error": f"{MAX_PROBE_URLS} URLs maximum par aperçu"}, status_code=400)
        return {"results": await run_in_threadpool(probe_urls, settings, urls)}

    @app.post("/products")
    async def product_create(request: Request):
        form = await request.form()
        _check_csrf(request, form.get("csrf_token"))
        urls = [u for u in str(form.get("urls", "")).splitlines() if u.strip()]
        urls += [str(u) for u in form.getlist("url") if str(u).strip()]  # cases cochées (recherche)
        try:
            with db() as (_, catalog):
                pid = catalog.create_product(str(form.get("name", "")),
                                             money_input(form.get("target_price"), "Seuil"), urls)
        except ConfigError as exc:
            return render(request, "product_new.html", status_code=400, error=str(exc),
                          form_name=form.get("name", ""), form_target=form.get("target_price", ""),
                          form_urls=form.get("urls", ""))
        if runner is not None and form.get("check_now"):
            runner.trigger([pid])
            return _go(f"/products/{pid}", ok="Article ajouté, première vérification en cours")
        return _go(f"/products/{pid}", ok="Article ajouté")

    @app.get("/products/{pid}", response_class=HTMLResponse)
    def product_detail(request: Request, pid: str):
        with db() as (store, catalog):
            product = catalog.product(pid)
            if product is None:
                raise HTTPException(404, "Article introuvable")
            failures = {u: (c, e) for u, c, e in store.failures()}
            summary = summarize(product, store, failures, runner, series_days=product.rules.lowest_window_days)
            rows = catalog.offers(pid)
            by_url = {o.url: o for o in product.offers}
            ctx = dict(
                product=product, summary=summary, rows=rows, by_url=by_url, failures=failures,
                overrides=catalog.rule_overrides(pid), defaults=catalog.effective_defaults(),
                alerts=store.alerts_for(pid), chart=price_chart(summary.series, product.target_cents),
            )
        return render(request, "product.html", **ctx)

    @app.post("/products/{pid}/edit")
    async def product_edit(request: Request, pid: str):
        form = await request.form()
        _check_csrf(request, form.get("csrf_token"))
        try:
            with db() as (_, catalog):
                catalog.update_product(pid, str(form.get("name", "")), money_input(form.get("target_price"), "Seuil"),
                                       bool(form.get("enabled")), _rules_from_form(form, inherit=True))
        except ConfigError as exc:
            return _go(f"/products/{pid}", err=str(exc))
        return _go(f"/products/{pid}", ok="Article mis à jour")

    @app.post("/products/{pid}/offers")
    async def offer_add(request: Request, pid: str):
        form = await request.form()
        _check_csrf(request, form.get("csrf_token"))
        try:
            with db() as (_, catalog):
                catalog.add_offer(pid, str(form.get("url", "")), store=str(form.get("store", "")).strip() or None,
                                  shipping_cents=money_input(form.get("shipping"), "Port"),
                                  free_above_cents=money_input(form.get("free_above"), "Seuil port offert"),
                                  selector=str(form.get("selector", "")).strip() or None,
                                  browser=True if form.get("browser") else None)
        except ConfigError as exc:
            return _go(f"/products/{pid}", err=str(exc))
        if runner is not None:
            runner.trigger([pid])
        return _go(f"/products/{pid}", ok="Offre ajoutée, vérification en cours")

    @app.post("/products/{pid}/offers/{oid}/edit")
    async def offer_edit(request: Request, pid: str, oid: int):
        form = await request.form()
        _check_csrf(request, form.get("csrf_token"))
        try:
            with db() as (_, catalog):
                catalog.update_offer(pid, oid, str(form.get("store", "")).strip() or None,
                                     money_input(form.get("shipping"), "Port"),
                                     money_input(form.get("free_above"), "Seuil port offert"),
                                     str(form.get("selector", "")).strip() or None,
                                     True if form.get("browser") else None)
        except ConfigError as exc:
            return _go(f"/products/{pid}", err=str(exc))
        return _go(f"/products/{pid}", ok="Offre mise à jour")

    @app.post("/products/{pid}/offers/{oid}/delete")
    async def offer_delete(request: Request, pid: str, oid: int):
        form = await request.form()
        _check_csrf(request, form.get("csrf_token"))
        try:
            with db() as (_, catalog):
                catalog.delete_offer(pid, oid)
        except ConfigError as exc:
            return _go(f"/products/{pid}", err=str(exc))
        return _go(f"/products/{pid}", ok="Offre supprimée")

    @app.post("/products/{pid}/check")
    async def product_check(request: Request, pid: str):
        form = await request.form()
        _check_csrf(request, form.get("csrf_token"))
        if runner is None:
            return _go(f"/products/{pid}", err="Planificateur indisponible dans ce mode")
        started = runner.trigger([pid])
        return _go(f"/products/{pid}", ok="Vérification lancée, recharge dans un instant" if started
                   else "Vérification déjà en cours")

    @app.post("/products/{pid}/delete")
    async def product_delete(request: Request, pid: str):
        form = await request.form()
        _check_csrf(request, form.get("csrf_token"))
        with db() as (store, catalog):
            catalog.delete_product(pid)
            if form.get("purge"):
                store.purge_product(pid)
        return _go("/", ok="Article supprimé")

    # --- boutiques -----------------------------------------------------------------------------

    @app.get("/stores", response_class=HTMLResponse)
    def stores(request: Request):
        with db() as (store, catalog):
            policies = catalog.store_policies()
            with_policy = {p.domain for p in policies}
            missing = [d for d in catalog.tracked_domains() if not any(d == w or d.endswith("." + w) for w in with_policy)]
            owners = catalog.products_by_url()
            failures = [(u, c, e, owners.get(u, [])) for u, c, e in store.failures()]
            default_shipping = catalog.default_shipping()
        return render(request, "stores.html", policies=policies, missing=missing, failures=failures,
                      default_shipping=default_shipping, threshold=settings.failure_threshold)

    @app.post("/stores")
    async def store_save(request: Request):
        form = await request.form()
        _check_csrf(request, form.get("csrf_token"))
        try:
            with db() as (_, catalog):
                catalog.set_store_policy(str(form.get("domain", "")), money_input(form.get("cost"), "Coût"),
                                         money_input(form.get("free_above"), "Port offert dès"),
                                         _parse_tiers(str(form.get("tiers", ""))), str(form.get("note", "")).strip())
        except ConfigError as exc:
            return _go("/stores", err=str(exc))
        return _go("/stores", ok=f"Boutique {form.get('domain')} enregistrée")

    @app.post("/stores/{domain}/delete")
    async def store_delete(request: Request, domain: str):
        form = await request.form()
        _check_csrf(request, form.get("csrf_token"))
        with db() as (_, catalog):
            catalog.delete_store_policy(domain)
        return _go("/stores", ok=f"Politique de {domain} supprimée")

    # --- réglages ------------------------------------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        with db() as (_, catalog):
            ctx = dict(overrides=catalog.defaults(), defaults=RuleSettings(),
                       default_shipping=catalog.default_shipping())
        ntfy_host = urlparse(os.environ.get("NTFY_URL", "")).hostname
        return render(request, "settings.html", settings=settings, ntfy_host=ntfy_host, **ctx)

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        _check_csrf(request, form.get("csrf_token"))
        try:
            with db() as (_, catalog):
                catalog.set_defaults(_rules_from_form(form, inherit=False),
                                     money_input(form.get("default_shipping"), "Port estimé"))
        except ConfigError as exc:
            return _go("/settings", err=str(exc))
        return _go("/settings", ok="Réglages enregistrés")

    @app.post("/settings/test-notify")
    async def settings_test_notify(request: Request):
        form = await request.form()
        _check_csrf(request, form.get("csrf_token"))
        notifiers = runner.notifiers if runner else []
        ok = await run_in_threadpool(dispatch, notifiers, Notification(
            "dealarr : test de notification", "Si tu lis ceci, la liaison fonctionne.", priority=3, tags=("test_tube",)))
        return _go("/settings", ok="Notification de test envoyée") if ok else \
            _go("/settings", err="Échec de l'envoi (voir les logs du service)")

    @app.get("/export.yaml")
    def export_yaml():
        with db() as (_, catalog):
            data = catalog.to_dict()
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        stamp = datetime.now().strftime("%Y%m%d")
        return PlainTextResponse(text, media_type="application/x-yaml", headers={
            "Content-Disposition": f'attachment; filename="dealarr-watchlist-{stamp}.yaml"'})

    @app.post("/import")
    async def import_yaml(request: Request):
        form = await request.form()
        _check_csrf(request, form.get("csrf_token"))
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return _go("/settings", err="Aucun fichier reçu")
        content = await upload.read(MAX_IMPORT_BYTES + 1)
        if len(content) > MAX_IMPORT_BYTES:
            return _go("/settings", err="Fichier trop volumineux (1 Mo max)")
        try:
            data = yaml.safe_load(content.decode("utf-8")) or {}
            with db() as (_, catalog):
                count = catalog.import_dict(data)
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            return _go("/settings", err=f"YAML illisible : {exc.__class__.__name__}")
        except ConfigError as exc:
            return _go("/settings", err=f"Import refusé : {exc}")
        return _go("/settings", ok=f"Watchlist importée : {count} article(s)")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__,
                "last_run": runner.last_run_ts if runner else None, "running": bool(runner and runner.running)}

    return app
