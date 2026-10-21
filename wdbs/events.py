"""Event bus (v0.1: durable log + in-process subscribers).

Every event is appended to the WDBS state DB `events` table, so any process
(WDBS Home, an AI station manager, a future Presentation Manager) can follow
the log by polling `id > last_seen`. In-process subscribers are notified
synchronously. A networked transport is deliberately out of scope until a port
is approved.

Event kinds emitted today:
    player.online / player.offline      recorder saw field_player start/stop
    airing.started / airing.ended       an episode went on / came off air
    replan.applied / replan.failed      guarded schedule regeneration
    show.activated / show.deactivated   operator override for the policy engine
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Callable

from .state import StateStore

Handler = Callable[[dict], None]


class EventBus:
    def __init__(self, store: StateStore):
        self.store = store
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, kind: str, handler: Handler):
        """kind may be exact ('airing.started') or a prefix wildcard ('airing.*' / '*')."""
        self._subs[kind].append(handler)

    def publish(self, kind: str, payload: dict | None = None, channel: int | None = None) -> int:
        event_id = self.store.emit(kind, payload, channel=channel)
        event = {"id": event_id, "kind": kind, "channel": channel, "payload": payload or {}}
        for pattern, handlers in list(self._subs.items()):
            if pattern == "*" or pattern == kind or (pattern.endswith(".*") and kind.startswith(pattern[:-1])):
                for h in handlers:
                    h(event)
        return event_id

    def since(self, last_id: int, limit: int = 200) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT id, ts, kind, channel, payload FROM events WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, limit)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"] or "{}")} for r in rows]
