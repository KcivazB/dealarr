from __future__ import annotations

import sys

from .base import Notification


class ConsoleNotifier:
    name = "console"

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout

    def send(self, notification: Notification) -> None:
        n = notification
        lines = [f"\n=== [{n.kind} p{n.priority}] {n.title}", n.message]
        if n.url:
            lines.append(f"→ {n.url}")
        for label, url in n.actions:
            lines.append(f"  [{label}] {url}")
        print("\n".join(lines), file=self.stream, flush=True)
