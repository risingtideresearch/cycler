"""Minimal Discord webhook poster, shared by the cycler and the monitor.

Blocking (uses urllib); callers on the event loop should wrap it in
asyncio.to_thread and treat it as best-effort (a failed/slow webhook must never
affect instrument control or monitoring).
"""
from __future__ import annotations

import json
import urllib.request


def discord_post(webhook: str, content: str) -> None:
    data = json.dumps({"content": content[:1900]}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()
