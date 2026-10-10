"""Central event log: the spine every subsystem writes to.

Trades, agent messages, heartbeat results and errors are appended as typed
events to a JSONL file and fanned out to live subscribers (the UI's SSE feed).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any


class EventLog:
    def __init__(self, path: Path, keep: int = 500):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._recent: deque[dict[str, Any]] = deque(maxlen=keep)
        self._subs: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
        if self.path.exists():
            lines = self.path.read_text(encoding="utf-8").splitlines()[-keep:]
            for line in lines:
                try:
                    self._recent.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    def emit(self, type_: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {"id": uuid.uuid4().hex[:12], "ts": time.time(), "type": type_, "payload": payload or {}}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
        self._recent.append(event)
        for loop, queue in list(self._subs):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except (RuntimeError, asyncio.QueueFull):
                self._subs.discard((loop, queue))
        return event

    def recent(self, limit: int = 100, prefix: str | None = None) -> list[dict[str, Any]]:
        items = [e for e in self._recent if not prefix or e["type"].startswith(prefix)]
        return items[-limit:]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subs = {s for s in self._subs if s[1] is not queue}
