"""Canaux de notification. Ajouter un canal = une classe avec `name` et `send(Notification)`."""

from __future__ import annotations

import logging
import os
from typing import Mapping

from .base import Notification, Notifier, NotifyError
from .console import ConsoleNotifier
from .ntfy import NtfyNotifier

log = logging.getLogger(__name__)

__all__ = ["Notification", "Notifier", "NotifyError", "build_notifiers", "dispatch"]


def build_notifiers(names: tuple[str, ...], env: Mapping[str, str] = os.environ) -> list[Notifier]:
    registry = {
        "console": lambda: ConsoleNotifier(),
        "ntfy": lambda: NtfyNotifier.from_env(env),
    }
    notifiers = []
    for name in names:
        if name not in registry:
            raise ValueError(f"Notifier inconnu : {name} (disponibles : {', '.join(registry)})")
        notifiers.append(registry[name]())
    return notifiers


def dispatch(notifiers: list[Notifier], notification: Notification) -> bool:
    """Envoie sur tous les canaux. True si au moins un a réussi."""
    ok = False
    for n in notifiers:
        try:
            n.send(notification)
            ok = True
        except NotifyError as exc:
            log.error("Échec d'envoi via %s : %s", n.name, exc)
    return ok
