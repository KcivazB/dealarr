"""Récupération des pages : HTTP simple par défaut, navigateur headless (Playwright) en option."""

from __future__ import annotations

import ipaddress
import logging
import random
import socket
import time
from collections.abc import Callable

import httpx

from .sites import domain_of

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6",
}


class FetchError(Exception):
    def __init__(self, message: str, blocked: bool = False):
        super().__init__(message)
        self.blocked = blocked


def assert_public_host(host: str | None) -> None:
    """Refuse les cibles non publiques (anti-SSRF) : localhost, réseaux privés, link-local, etc.

    Les URLs sont saisies par l'utilisateur (watchlist, interface web) et le service tourne dans
    le homelab : sans ce garde-fou, il pourrait interroger des services internes.
    """
    if not host:
        raise FetchError("URL sans hôte")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError(f"DNS : hôte introuvable ({host})") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise FetchError(f"adresse non publique refusée ({host})")


class Fetcher:
    def __init__(
        self,
        domain_delay: tuple[float, float] = (4.0, 10.0),
        browser_enabled: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.domain_delay = domain_delay
        self.browser_enabled = browser_enabled
        self._sleep = sleep
        self._last_hit: dict[str, float] = {}
        # Le hook s'applique aussi à chaque redirection suivie.
        self._client = httpx.Client(
            headers=_HEADERS, timeout=25, follow_redirects=True, max_redirects=5,
            event_hooks={"request": [lambda request: assert_public_host(request.url.host)]},
        )
        self._pw = None
        self._browser = None

    def close(self) -> None:
        self._client.close()
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _throttle(self, url: str) -> None:
        """Espace les requêtes vers un même domaine (politesse + discrétion)."""
        domain = domain_of(url)
        last = self._last_hit.get(domain)
        if last is not None:
            wait = random.uniform(*self.domain_delay) - (time.monotonic() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_hit[domain] = time.monotonic()

    def get(self, url: str, browser: bool = False) -> str:
        self._throttle(url)
        if browser:
            return self._get_browser(url)
        if not url.startswith(("https://", "http://")):
            raise FetchError("seules les URLs http(s) sont acceptées")
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as exc:
            raise FetchError(f"erreur réseau : {exc.__class__.__name__}") from exc
        if resp.status_code in (403, 429) or (resp.status_code == 503 and "captcha" in resp.text.lower()):
            raise FetchError(f"HTTP {resp.status_code} (probable anti-bot)", blocked=True)
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code}")
        return resp.text

    def _get_browser(self, url: str) -> str:
        from urllib.parse import urlparse

        assert_public_host(urlparse(url).hostname)
        if not self.browser_enabled:
            raise FetchError("navigateur requis mais désactivé (DEALARR_BROWSER=true + image 'browser')")
        if self._browser is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise FetchError("Playwright n'est pas installé (pip install 'dealarr[browser]')") from exc
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        context = self._browser.new_context(locale="fr-FR", user_agent=USER_AGENT, viewport={"width": 1366, "height": 900})
        try:
            page = context.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:  # pages avec trackers qui ne se taisent jamais
                pass
            status = resp.status if resp else 0
            if status in (403, 429):
                raise FetchError(f"HTTP {status} (probable anti-bot, navigateur)", blocked=True)
            if status >= 400:
                raise FetchError(f"HTTP {status} (navigateur)")
            return page.content()
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError(f"navigateur : {exc.__class__.__name__}") from exc
        finally:
            context.close()
