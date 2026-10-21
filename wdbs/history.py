"""Derived playout metrics per program (show tag) from recorded airings.

An airing "counts" once at least QUALIFY_FRACTION of the episode was on air;
tune-ins that last a few seconds do not burn an episode.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

from . import fs42_adapter as fs
from .episodes import parse_episode
from .state import StateStore, now_str

QUALIFY_FRACTION = 0.5


@dataclass
class ProgramHistory:
    tag: str
    channel: int
    last_aired: str | None = None
    first_aired: str | None = None
    airings_7d: int = 0
    airings_30d: int = 0
    airings_total: int = 0
    unique_episodes_aired: int = 0
    episodes_in_library: int = 0
    unplayed_episodes: int = 0
    last_episode_aired: str | None = None
    next_episode: str | None = None
    next_scheduled_at: str | None = None
    cooldown_until: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    def hours_since_last_aired(self, now: dt.datetime) -> float | None:
        if self.last_aired is None:
            return None
        return (now - fs.parse_ts(self.last_aired)).total_seconds() / 3600.0


def _qualified_sql() -> str:
    return ("(completed = 1 OR (feature_duration_s > 0 AND position_s >= feature_duration_s * "
            f"{QUALIFY_FRACTION}))")


def _label(title: str | None, path: str | None) -> str | None:
    ep = parse_episode(path or title)
    base = title or (path.rsplit("/", 1)[-1] if path else None)
    if ep.code and base and ep.code not in base.upper():
        return f"{ep.code} {base}"
    return base


def program_history(store: StateStore, channel: int, tag: str, *, now: dt.datetime,
                    library_paths: list[str] | None = None,
                    cooldown_hours: float | None = None,
                    next_block: fs.Block | None = None,
                    next_sequence_path: str | None = None) -> ProgramHistory:
    q = _qualified_sql()
    c = store.conn
    h = ProgramHistory(tag=tag, channel=channel)

    row = c.execute(
        f"SELECT started_at, title, file_path FROM airings WHERE channel = ? AND tag = ? AND {q} "
        "ORDER BY started_at DESC LIMIT 1", (channel, tag)).fetchone()
    if row:
        h.last_aired = row["started_at"]
        h.last_episode_aired = _label(row["title"], row["file_path"])

    row = c.execute(f"SELECT min(started_at) FROM airings WHERE channel = ? AND tag = ? AND {q}",
                    (channel, tag)).fetchone()
    h.first_aired = row[0] if row else None

    def count_since(delta: dt.timedelta | None) -> int:
        sql = f"SELECT count(*) FROM airings WHERE channel = ? AND tag = ? AND {q}"
        args: list = [channel, tag]
        if delta is not None:
            sql += " AND started_at >= ?"
            args.append(now_str(now - delta))
        return c.execute(sql, args).fetchone()[0]

    h.airings_7d = count_since(dt.timedelta(days=7))
    h.airings_30d = count_since(dt.timedelta(days=30))
    h.airings_total = count_since(None)

    aired_paths = {r[0] for r in c.execute(
        f"SELECT DISTINCT file_path FROM airings WHERE channel = ? AND tag = ? AND {q}", (channel, tag))}
    h.unique_episodes_aired = len(aired_paths)
    if library_paths is not None:
        lib = set(library_paths)
        h.episodes_in_library = len(lib)
        h.unplayed_episodes = len(lib - aired_paths)

    if next_block is not None:
        h.next_scheduled_at = now_str(next_block.start)
        h.next_episode = next_block.title
    elif next_sequence_path:
        h.next_episode = _label(None, next_sequence_path)

    if h.last_aired and cooldown_hours:
        until = fs.parse_ts(h.last_aired) + dt.timedelta(hours=cooldown_hours)
        h.cooldown_until = now_str(until) if until > now else None
    return h


def recent_airings(store: StateStore, channel: int | None = None, limit: int = 20) -> list[dict]:
    sql = "SELECT * FROM airings"
    args: list = []
    if channel is not None:
        sql += " WHERE channel = ?"
        args.append(channel)
    sql += " ORDER BY started_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in store.conn.execute(sql, args)]
