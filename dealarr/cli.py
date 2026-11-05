"""Interface en ligne de commande."""

from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import yaml

from . import __version__
from .catalog import Catalog
from .config import ConfigError, Settings
from .runner import Runner, bootstrap_catalog
from .extract import extract, looks_blocked
from .fetch import FetchError, Fetcher
from .money import fmt
from .notify import Notification, build_notifiers, dispatch
from .sites import canonical_url, profile_for, store_name
from .storage import Store

log = logging.getLogger("dealarr")


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Chargement minimal d'un .env local (sans écraser l'environnement existant)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    # httpx logge les URLs complètes en INFO : on coupe pour ne jamais exposer de clé (Keepa...).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _products(settings: Settings):
    store = Store(settings.db_path)
    try:
        return Catalog(store.conn).load_products()
    finally:
        store.close()


def cmd_check(settings: Settings, args) -> int:
    bootstrap_catalog(settings)
    notifiers = build_notifiers(("console",) if args.dry_run else settings.notifiers)
    Runner(settings, notifiers).run()
    return 0


def _install_stop_handlers(stop: threading.Event) -> None:
    def _stop(*_):
        log.info("Arrêt demandé…")
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)


def cmd_run(settings: Settings, args) -> int:
    bootstrap_catalog(settings)
    runner = Runner(settings)
    stop = threading.Event()
    _install_stop_handlers(stop)
    log.info("dealarr %s : vérification toutes les ~%d min (notifiers : %s)",
             __version__, settings.interval_minutes, ", ".join(settings.notifiers))
    runner.loop(stop)
    return 0


def cmd_serve(settings: Settings, args) -> int:
    import uvicorn

    from .web.app import create_app

    bootstrap_catalog(settings)
    runner = Runner(settings)
    stop = threading.Event()
    if not args.no_scheduler:
        runner.start_scheduler(stop)
    host = args.host or os.environ.get("DEALARR_HOST", "127.0.0.1")
    port = int(args.port or os.environ.get("DEALARR_PORT", "8080"))
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("Interface exposée sur %s:%d SANS authentification intégrée : "
                    "à placer derrière un proxy authentifiant (Authentik…).", host, port)
    log.info("dealarr %s : interface sur http://%s:%d", __version__, host, port)
    try:
        uvicorn.run(create_app(settings, runner), host=host, port=port, log_level="warning",
                    proxy_headers=True, forwarded_allow_ips=os.environ.get("DEALARR_TRUSTED_PROXIES", "127.0.0.1"))
    finally:
        stop.set()
    return 0


def cmd_import(settings: Settings, args) -> int:
    path = Path(args.path or settings.watchlist_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    store = Store(settings.db_path)
    try:
        catalog = Catalog(store.conn)
        if not catalog.is_empty() and not args.replace:
            print("La base contient déjà une watchlist : ajoute --replace pour la remplacer.", file=sys.stderr)
            return 1
        count = catalog.import_dict(data)
    finally:
        store.close()
    print(f"Importé : {count} produit(s) depuis {path}")
    return 0


def cmd_export(settings: Settings, args) -> int:
    store = Store(settings.db_path)
    try:
        text = yaml.safe_dump(Catalog(store.conn).to_dict(), allow_unicode=True, sort_keys=False)
    finally:
        store.close()
    if args.path:
        Path(args.path).write_text(text, encoding="utf-8")
        print(f"Exporté vers {args.path}")
    else:
        print(text)
    return 0


def cmd_probe(settings: Settings, args) -> int:
    url = canonical_url(args.url)
    profile = profile_for(url)
    print(f"Boutique : {store_name(url)}  (profil : {'oui' if profile else 'générique'})")
    with Fetcher((0, 0), browser_enabled=settings.browser_enabled or args.browser) as fetcher:
        try:
            html = fetcher.get(url, browser=args.browser)
        except FetchError as exc:
            print(f"ÉCHEC récupération : {exc}")
            return 2
    ex = extract(html, profile, args.selector)
    print(f"Titre    : {ex.title}")
    print(f"Prix     : {fmt(ex.price_cents)}  (méthode : {ex.method})")
    print(f"Stock    : {ex.in_stock}")
    if not ex.ok:
        print("→ page anti-bot détectée : site non pris en charge" if looks_blocked(html)
              else "→ prix non trouvé : ajoute un 'selector' CSS pour cette offre")
        return 1
    if args.keepa:
        from .keepa import fetch_stats
        from .sites import amazon_asin, domain_of

        asin = amazon_asin(url)
        if not (asin and settings.keepa_api_key):
            print("Keepa : URL non Amazon ou KEEPA_API_KEY absente")
        else:
            stats = fetch_stats(settings.keepa_api_key, asin, domain_of(url))
            print(f"Keepa    : plus bas 90 j {fmt(stats.low_cents)}, moyenne 90 j {fmt(stats.avg_cents)}" if stats
                  else "Keepa    : pas de données")
    return 0


def cmd_status(settings: Settings, args) -> int:
    bootstrap_catalog(settings)
    products = _products(settings)
    store = Store(settings.db_path)
    now = int(time.time())
    try:
        for p in products:
            r = p.rules
            days, low, median = store.history_summary(p.id, now + 1, r.lowest_window_days, r.median_window_days, r.require_in_stock)
            state = store.get_alert_state(p.id)
            flag = f"  🔔 alerté à {fmt(state[0])}" if state else ""
            print(f"\n{p.name} [{p.id}]{'' if p.enabled else ' (désactivé)'}  seuil {fmt(p.target_cents)}"
                  f"  | plus bas {fmt(low)}  médiane {fmt(median)}  ({days} j){flag}")
            for o in store.latest_by_offer(p.id):
                stock = {True: "", False: " rupture", None: " stock ?"}[o.in_stock]
                when = datetime.fromtimestamp(o.ts).strftime("%d/%m %H:%M")
                port = o.total_cents - o.price_cents
                port_txt = f"(article {fmt(o.price_cents)} + port {fmt(port)})" if port else ""
                print(f"   {o.store:<16}{fmt(o.total_cents):>12}{stock:<9} {when}  {port_txt}")
        for url, count, err in store.failures():
            print(f"\n⚠ {count} échec(s) : {url}\n   {err}")
    finally:
        store.close()
    return 0


def cmd_history(settings: Settings, args) -> int:
    store = Store(settings.db_path)
    now = int(time.time())
    try:
        rows = store.daily_best(args.product_id, now - args.days * 86400, now + 1, in_stock_only=False)
    finally:
        store.close()
    if not rows:
        print("Aucun relevé.")
        return 1
    top = max(p for _, p in rows)
    for day, price in rows:
        print(f"{day}  {fmt(price):>12}  {'█' * max(1, round(price / top * 40))}")
    return 0


def cmd_test_notify(settings: Settings, args) -> int:
    notifiers = build_notifiers(settings.notifiers)
    ok = dispatch(notifiers, Notification(
        title="dealarr : test de notification",
        message="Si tu lis ceci, la liaison fonctionne.",
        priority=3, tags=("test_tube",),
    ))
    print("Envoyé." if ok else "Échec (voir logs).")
    return 0 if ok else 1


def cmd_validate(settings: Settings, args) -> int:
    bootstrap_catalog(settings)
    products = _products(settings)
    offers = sum(len(p.offers) for p in products)
    print(f"OK : {len(products)} produit(s), {offers} offre(s)")
    for p in products:
        stores = ", ".join(o.store for o in p.offers)
        print(f"  - {p.id}: {p.name}  seuil {fmt(p.target_cents)}  [{stores}]")
    build_notifiers(settings.notifiers)  # vérifie la config des notifiers
    print(f"Notifiers : {', '.join(settings.notifiers)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dealarr", description="Suivi de prix ciblé avec alertes.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="lance un cycle de vérification (pour cron / CronJob)")
    p.add_argument("--dry-run", action="store_true", help="notifications en console uniquement")
    p.set_defaults(func=cmd_check)

    sub.add_parser("run", help="boucle de vérification sans interface").set_defaults(func=cmd_run)

    p = sub.add_parser("serve", help="interface web + vérifications planifiées (mode service)")
    p.add_argument("--host", help="adresse d'écoute (défaut : DEALARR_HOST ou 127.0.0.1)")
    p.add_argument("--port", type=int, help="port (défaut : DEALARR_PORT ou 8080)")
    p.add_argument("--no-scheduler", action="store_true", help="interface seule, sans vérifications planifiées")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("import-yaml", help="importe une watchlist YAML dans la base")
    p.add_argument("path", nargs="?", help="fichier (défaut : DEALARR_WATCHLIST)")
    p.add_argument("--replace", action="store_true", help="remplacer la watchlist existante")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("export-yaml", help="exporte la watchlist de la base en YAML")
    p.add_argument("path", nargs="?", help="fichier de sortie (défaut : stdout)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("probe", help="teste l'extraction sur une URL")
    p.add_argument("url")
    p.add_argument("--browser", action="store_true", help="utiliser le navigateur headless")
    p.add_argument("--selector", help="sélecteur CSS du prix à tester")
    p.add_argument("--keepa", action="store_true", help="interroger aussi Keepa (Amazon)")
    p.set_defaults(func=cmd_probe)

    sub.add_parser("status", help="dernier prix par boutique et historique").set_defaults(func=cmd_status)

    p = sub.add_parser("history", help="meilleur prix par jour pour un produit")
    p.add_argument("product_id")
    p.add_argument("--days", type=int, default=60)
    p.set_defaults(func=cmd_history)

    sub.add_parser("test-notify", help="envoie une notification de test").set_defaults(func=cmd_test_notify)
    sub.add_parser("validate", help="vérifie la watchlist et la config").set_defaults(func=cmd_validate)

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # consoles Windows en cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    _load_dotenv()
    try:
        settings = Settings.from_env()
        _setup_logging(settings.log_level)
        return args.func(settings, args)
    except (ConfigError, ValueError) as exc:
        print(f"Erreur de configuration : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
