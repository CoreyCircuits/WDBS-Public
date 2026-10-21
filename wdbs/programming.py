"""Programming inputs: the Notion-derived rotation manifest, local overrides,
and per-program facts gathered from FS42 + playout history."""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import fs42_adapter as fs
from .history import program_history
from .policy import ProgramFacts, ProgramPolicy
from .state import StateStore

MANIFEST_STALE_AFTER = dt.timedelta(hours=24)


@dataclass
class Manifest:
    path: Path
    channel: int | None
    generated_at: dt.datetime | None
    programs: list[dict]
    error: str | None = None

    def age(self, now_utc: dt.datetime | None = None) -> dt.timedelta | None:
        if not self.generated_at:
            return None
        now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
        return now_utc - self.generated_at

    @property
    def stale(self) -> bool:
        a = self.age()
        return a is None or a > MANIFEST_STALE_AFTER


def load_manifest(path: Path) -> Manifest:
    try:
        obj = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        return Manifest(Path(path), None, None, [], error=str(exc))
    gen = None
    if obj.get("generated_at"):
        try:
            gen = dt.datetime.fromisoformat(obj["generated_at"].replace("Z", "+00:00"))
        except ValueError:
            gen = None
    return Manifest(Path(path), obj.get("channel"), gen, list(obj.get("programs") or []))


def rotation_policies(manifest: Manifest, store: StateStore, channel: int) -> list[ProgramPolicy]:
    """Manifest programs with local overrides applied. Deactivated programs stay
    in the list with in_rotation=False so reasons can mention them."""
    overrides = store.get_overrides(channel)
    out = []
    for item in manifest.programs:
        if not item.get("tag"):
            continue
        p = ProgramPolicy.from_manifest(item)
        ov = overrides.get(p.tag)
        if ov:
            if ov.get("active") == 0:
                p.in_rotation = False
                p.inactive_reason = "deactivated by operator" + (f": {ov['note']}" if ov.get("note") else "")
            if ov.get("manual_boost") is not None:
                p.manual_boost = float(ov["manual_boost"])
        out.append(p)
    return out


def library_paths(catalog: dict[int, fs.CatalogEntry], tag: str) -> list[str]:
    return sorted(e.path for e in catalog.values() if e.tag == tag and e.content_type == "feature")


def gather_facts(conn, store: StateStore, station: fs.Station, policies: list[ProgramPolicy],
                 now: dt.datetime, check_media: bool = True) -> tuple[dict[str, ProgramFacts], dict]:
    catalog = fs.get_catalog(conn, station.network_name)
    sequences = fs.get_sequences(conn, station.network_name)
    upcoming = fs.get_blocks(conn, station.network_name, now, now + dt.timedelta(days=14))
    facts: dict[str, ProgramFacts] = {}
    histories = {}
    for p in policies:
        paths = library_paths(catalog, p.tag)
        f = ProgramFacts()
        f.episodes_in_library = len(paths)
        f.catalog_ready = bool(paths)
        if not paths:
            f.notes.append("no catalog entries")
        seq = sequences.get((p.tag, p.tag))
        f.sequence_ready = seq is not None and bool(seq.episodes)
        tag_dir = Path(station.content_dir or "") / p.tag
        if check_media:
            missing = [x for x in paths if not os.path.exists(x)]
            f.media_available = tag_dir.exists() and bool(paths) and not missing
            if missing:
                f.notes.append(f"{len(missing)} cataloged file(s) missing on disk")
        nb = next((b for b in upcoming if b.start >= now and b.tag_path == p.tag), None)
        h = program_history(store, station.channel, p.tag, now=now, library_paths=paths,
                            cooldown_hours=p.min_repeat_gap_hours, next_block=nb,
                            next_sequence_path=seq.next_path() if seq else None)
        f.last_aired = fs.parse_ts(h.last_aired) if h.last_aired else None
        f.airings_7d, f.airings_30d = h.airings_7d, h.airings_30d
        f.unplayed_episodes = h.unplayed_episodes
        f.cooldown_until = fs.parse_ts(h.cooldown_until) if h.cooldown_until else None
        facts[p.tag] = f
        histories[p.tag] = h
    return facts, histories
