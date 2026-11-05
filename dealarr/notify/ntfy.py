"""Publication ntfy via l'API JSON (POST sur la racine du serveur).

Variables d'environnement :
  NTFY_URL            ex. https://ntfy.mondomaine.lan (obligatoire)
  NTFY_TOPIC          topic des bons plans (obligatoire)
  NTFY_TOPIC_SYSTEM   topic des alertes techniques (défaut : NTFY_TOPIC)
  NTFY_TOKEN          jeton d'accès (recommandé)  — ou —
  NTFY_USER / NTFY_PASSWORD  authentification basique
"""

from __future__ import annotations

import os
from typing import Mapping

import httpx

from .base import Notification, NotifyError


class NtfyNotifier:
    name = "ntfy"

    def __init__(
        self,
        base_url: str,
        topic: str,
        system_topic: str | None = None,
        token: str | None = None,
        basic_auth: tuple[str, str] | None = None,
        client: httpx.Client | None = None,
    ):
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("NTFY_URL doit commencer par http(s)://")
        self.base_url = base_url.rstrip("/")
        self.topic = topic
        self.system_topic = system_topic or topic
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = client or httpx.Client(timeout=15)
        self._headers = headers
        self._auth = httpx.BasicAuth(*basic_auth) if basic_auth and not token else None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "NtfyNotifier":
        url, topic = env.get("NTFY_URL", "").strip(), env.get("NTFY_TOPIC", "").strip()
        if not url or not topic:
            raise ValueError("NTFY_URL et NTFY_TOPIC sont requis pour le notifier ntfy")
        user, password = env.get("NTFY_USER", "").strip(), env.get("NTFY_PASSWORD", "")
        return cls(
            base_url=url,
            topic=topic,
            system_topic=env.get("NTFY_TOPIC_SYSTEM", "").strip() or None,
            token=env.get("NTFY_TOKEN", "").strip() or None,
            basic_auth=(user, password) if user and password else None,
        )

    def payload(self, n: Notification) -> dict:
        body: dict = {
            "topic": self.system_topic if n.kind == "system" else self.topic,
            "title": n.title,
            "message": n.message,
            "priority": max(1, min(5, n.priority)),
        }
        if n.tags:
            body["tags"] = list(n.tags)
        if n.url:
            body["click"] = n.url
        if n.actions:
            body["actions"] = [{"action": "view", "label": label, "url": url} for label, url in n.actions[:3]]
        return body

    def send(self, notification: Notification) -> None:
        try:
            resp = self.client.post(
                self.base_url, json=self.payload(notification), headers=self._headers, auth=self._auth
            )
        except httpx.HTTPError as exc:
            raise NotifyError(f"ntfy injoignable : {exc.__class__.__name__}") from exc
        if resp.status_code >= 400:
            # On ne logge pas le corps de la requête (pas de fuite d'en-têtes/jetons).
            raise NotifyError(f"ntfy a répondu HTTP {resp.status_code}: {resp.text[:200]}")
