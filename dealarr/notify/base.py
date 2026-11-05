from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class NotifyError(Exception):
    pass


@dataclass(frozen=True)
class Notification:
    title: str
    message: str
    priority: int = 3  # 1 (min) .. 5 (max), échelle ntfy
    tags: tuple[str, ...] = ()
    url: str | None = None  # lien ouvert au clic
    actions: tuple[tuple[str, str], ...] = field(default=())  # (libellé, url), 3 max
    kind: str = "deal"  # deal | system


class Notifier(Protocol):
    name: str

    def send(self, notification: Notification) -> None: ...
