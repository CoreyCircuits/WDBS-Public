"""Presentation primitives - extension points for WDBS Broadcast OS.

WDBS presents five kinds of things. v0.1 defines their contracts only; the
Presentation Manager that arbitrates them is future work. FieldStation42 is the
linear-TV playout subsystem behind `Channel`; it is not the system-wide
control plane.

    Channel   continuous linear programming (FS42 station today)
    App       interactive full-screen experience the viewer tunes to
    Overlay   layered on top of whatever is on screen (ticker, bug, now-playing)
    Takeover  temporarily pre-empts a channel (breaking news, event, PSA)
    Scene     a named composition of the above (e.g. "Saturday Morning")

Arbitration rule for the future Presentation Manager (documented, not built):
Takeover > App (if tuned) > Channel; Overlays stack above all, ordered by
priority; a Scene only declares a desired composition and must pass the same
validation as any other change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class PresentationRef:
    kind: str            # channel | app | overlay | takeover | scene
    id: str              # stable identifier, e.g. "channel:10"
    title: str
    meta: dict = field(default_factory=dict)


@runtime_checkable
class Presentable(Protocol):
    def ref(self) -> PresentationRef: ...
    def describe(self) -> dict: ...


@runtime_checkable
class Channel(Presentable, Protocol):
    number: int
    def now(self) -> dict: ...
    def upcoming(self, count: int) -> list[dict]: ...


@runtime_checkable
class App(Presentable, Protocol):
    def launch(self, context: dict) -> None: ...
    def exit(self) -> None: ...


@runtime_checkable
class Overlay(Presentable, Protocol):
    priority: int
    def show(self, payload: dict) -> None: ...
    def hide(self) -> None: ...


@runtime_checkable
class Takeover(Presentable, Protocol):
    def begin(self, channel: int, reason: str) -> None: ...
    def end(self) -> None: ...


@runtime_checkable
class Scene(Presentable, Protocol):
    def composition(self) -> list[PresentationRef]: ...


class FS42Channel:
    """A Channel backed by a FieldStation42 standard station via the control plane."""

    def __init__(self, control_plane, number: int):
        self.cp = control_plane
        self.number = number

    def ref(self) -> PresentationRef:
        s = self.cp._station(self.number)
        return PresentationRef("channel", f"channel:{self.number}", s.network_name,
                               {"engine": "fieldstation42", "network_type": s.network_type})

    def describe(self) -> dict:
        return {"ref": self.ref().__dict__, "now": self.now()}

    def now(self) -> dict:
        return self.cp.now(self.number)

    def upcoming(self, count: int = 3) -> list[dict]:
        return self.cp.next(self.number, count)["next"]


class Registry:
    """Where the Presentation Manager will find everything it can put on screen."""

    def __init__(self):
        self._items: dict[str, Presentable] = {}

    def register(self, item: Presentable):
        self._items[item.ref().id] = item

    def get(self, ref_id: str) -> Presentable | None:
        return self._items.get(ref_id)

    def list(self, kind: str | None = None) -> list[PresentationRef]:
        refs = [i.ref() for i in self._items.values()]
        return [r for r in refs if kind is None or r.kind == kind]
