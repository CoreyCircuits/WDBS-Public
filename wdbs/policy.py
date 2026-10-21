"""WDBS programming policy engine.

Pure, deterministic, and explainable: given program policies, facts about each
program, and the slots to fill, it produces a lineup where every slot carries an
Algorithm Reason. It never touches FS42 or the network.

Hard gates remove a program from consideration. Some gates are *relaxable*: if
a slot would otherwise be empty, the engine relaxes them in a fixed order and
records that in the reason. Non-relaxable gates are never bypassed.

Soft scoring ranks the programs that passed. Each score is a sum of named
components so the reason can show exactly why a program won.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import asdict, dataclass, field

DAYPARTS = [
    (0, 5, "Overnight"),
    (5, 7, "Early"),
    (7, 11, "Morning"),
    (11, 15, "Daytime"),
    (15, 18, "After School"),
    (18, 22, "Prime"),
    (22, 24, "Late Night"),
]


def daypart(hour: int) -> str:
    for lo, hi, name in DAYPARTS:
        if lo <= hour < hi:
            return name
    raise ValueError(hour)


# Tunable scoring constants. Changing these changes lineups; keep them visible.
W_BALANCE = 3.0          # penalty per weighted assignment already made today
W_RECENCY = 2.0          # bonus scaled by hours since last aired (capped)
RECENCY_CAP_H = 48.0
W_INVENTORY = 1.5        # bonus scaled by unplayed episode inventory (capped)
INVENTORY_CAP = 20
W_EXPOSURE_7D = 0.05
W_EXPOSURE_30D = 0.01
W_WEIGHT = 0.5
W_WDBS_RATING = 0.2
W_IMDB = 0.4
IMDB_PRIOR_MEAN = 7.0
IMDB_PRIOR_VOTES = 5000
SMALL_LIBRARY = 10
W_SMALL_LIBRARY = 1.0
# Day-to-day rotation: penalty when a show held this time of day on one of the last few days.
# Index 0 = yesterday. The other half of the same hour counts at SAME_HOUR_FACTOR.
W_SAME_TIME = (1.5, 0.75, 0.4)
SAME_HOUR_FACTOR = 0.5


@dataclass
class ProgramPolicy:
    tag: str
    program: str
    notion_page_id: str | None = None
    dayparts: list[str] = field(default_factory=list)
    weight: float = 1.0
    min_repeat_gap_hours: float = 1.0
    max_per_day: int | None = None
    in_rotation: bool = True
    wdbs_rating: float | None = None
    imdb_rating: float | None = None
    imdb_votes: int | None = None
    manual_boost: float = 0.0
    inactive_reason: str | None = None

    @classmethod
    def from_manifest(cls, item: dict) -> "ProgramPolicy":
        def num(key, default=None, cast=float):
            v = item.get(key)
            try:
                return cast(v) if v is not None else default
            except (TypeError, ValueError):
                return default
        return cls(
            tag=item["tag"],
            program=item.get("program") or item["tag"],
            notion_page_id=item.get("notion_page_id"),
            dayparts=list(item.get("dayparts") or []),
            weight=max(num("weight", 1.0), 0.01),
            min_repeat_gap_hours=num("min_repeat_gap_hours", 1.0),
            max_per_day=num("max_per_day", None, int),
            wdbs_rating=num("wdbs_rating"),
            imdb_rating=num("imdb_rating"),
            imdb_votes=num("imdb_votes", None, int),
            manual_boost=num("manual_boost", 0.0),
        )


@dataclass
class ProgramFacts:
    media_available: bool = True
    catalog_ready: bool = True
    sequence_ready: bool = True
    episodes_in_library: int | None = None
    unplayed_episodes: int | None = None
    last_aired: dt.datetime | None = None
    airings_7d: int = 0
    airings_30d: int = 0
    cooldown_until: dt.datetime | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Slot:
    start: dt.datetime
    locked_tag: str | None = None
    lock_reason: str | None = None
    # Airtime in hours. Counts, Max Per Day and balance are all in hours, so a Max Per Day
    # of 1 means one hour a day whether it is filled by hour or half-hour slots.
    length_h: float = 1.0

    @property
    def hour(self) -> int:
        return self.start.hour

    @property
    def daypart(self) -> str:
        return daypart(self.start.hour)


@dataclass
class Candidate:
    tag: str
    program: str
    score: float
    components: list[tuple[str, float, str]]


@dataclass
class SlotDecision:
    start: str
    hour: int
    daypart: str
    tag: str | None
    program: str | None
    score: float | None
    reason: str
    relaxed: list[str] = field(default_factory=list)
    components: list[tuple[str, float, str]] = field(default_factory=list)
    runners_up: list[dict] = field(default_factory=list)
    rejected: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


class PolicyError(Exception):
    pass


# Relaxation ladder. Level N relaxes every gate listed up to and including N.
RELAX_LADDER = [
    [],
    ["daypart"],
    ["daypart", "daypart_capped"],
    ["daypart", "daypart_capped", "repeat_gap", "cooldown"],
    ["daypart", "daypart_capped", "repeat_gap", "cooldown", "back_to_back"],
]
NON_RELAXABLE = ("in_rotation", "media", "catalog", "sequence", "max_per_day")


def _stable(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


def imdb_weighted(rating: float | None, votes: int | None) -> float | None:
    if rating is None:
        return None
    v = max(votes or 0, 0)
    m = IMDB_PRIOR_VOTES
    return (v / (v + m)) * rating + (m / (v + m)) * IMDB_PRIOR_MEAN


class _DayState:
    def __init__(self):
        self.counts: dict[str, float] = {}
        self.last_start: dict[str, dt.datetime] = {}
        self.previous_tag: str | None = None
        self.day: dt.date | None = None
        # tag -> {(date, minute of day)} of every start seen, for day-to-day rotation
        self.aired: dict[str, set[tuple[dt.date, int]]] = {}

    def roll_day(self, day: dt.date):
        if self.day != day:
            self.day = day
            self.counts = {}

    def assign(self, tag: str, start: dt.datetime, length_h: float = 1.0):
        self.roll_day(start.date())
        self.counts[tag] = self.counts.get(tag, 0) + length_h
        self.last_start[tag] = start
        self.previous_tag = tag
        self.aired.setdefault(tag, set()).add((start.date(), start.hour * 60 + start.minute))

    def same_time_penalty(self, tag: str, start: dt.datetime) -> tuple[float, list[str]]:
        seen = self.aired.get(tag) or set()
        minute = start.hour * 60 + start.minute
        other = start.hour * 60 + (30 if start.minute < 30 else 0)
        total, days = 0.0, []
        for back, w in enumerate(W_SAME_TIME, start=1):
            d = start.date() - dt.timedelta(days=back)
            if (d, minute) in seen:
                total += w
                days.append(f"{back}d ago")
            elif (d, other) in seen:
                total += w * SAME_HOUR_FACTOR
                days.append(f"{back}d ago (same hour)")
        return total, days


def gate_failures(p: ProgramPolicy, f: ProgramFacts, slot: Slot, st: _DayState) -> list[tuple[str, str]]:
    out = []
    if not p.in_rotation:
        out.append(("in_rotation", p.inactive_reason or "not In Rotation"))
    if not f.media_available:
        out.append(("media", "media unavailable"))
    if not f.catalog_ready:
        out.append(("catalog", "not catalog-ready"))
    if not f.sequence_ready:
        out.append(("sequence", "no FS42 sequence"))
    if p.max_per_day is not None and st.counts.get(p.tag, 0) >= p.max_per_day:
        out.append(("max_per_day", f"at Max Per Day ({p.max_per_day})"))
    if p.dayparts and slot.daypart not in p.dayparts:
        # A Max-Per-Day program is scarce: it is relaxed off-daypart only after uncapped ones.
        kind = "daypart_capped" if p.max_per_day is not None else "daypart"
        out.append((kind, f"not programmed for {slot.daypart} ({'/'.join(p.dayparts)})"
                    + (f"; Max Per Day {p.max_per_day} slot kept for its dayparts" if kind == "daypart_capped" else "")))
    last = st.last_start.get(p.tag)
    if last is not None and p.tag != st.previous_tag:
        gap_h = (slot.start - last).total_seconds() / 3600.0
        if gap_h < p.min_repeat_gap_hours:
            out.append(("repeat_gap", f"aired {gap_h:.1f}h ago (min gap {p.min_repeat_gap_hours:g}h)"))
    if f.cooldown_until and f.cooldown_until > slot.start and last is None:
        out.append(("cooldown", f"cooling down until {f.cooldown_until:%a %H:%M}"))
    if st.previous_tag == p.tag:
        out.append(("back_to_back", "would repeat the previous slot"))
    return out


def score(p: ProgramPolicy, f: ProgramFacts, slot: Slot, st: _DayState) -> Candidate:
    comps: list[tuple[str, float, str]] = []
    assigned = st.counts.get(p.tag, 0)
    comps.append(("balance", -W_BALANCE * assigned / p.weight,
                  f"{assigned:g} h today at weight {p.weight:g}"))
    same, when = st.same_time_penalty(p.tag, slot.start)
    if same:
        comps.append(("rotation", -same, f"held {slot.start:%H:%M} {', '.join(when)}"))

    last = st.last_start.get(p.tag) or f.last_aired
    if last is None:
        comps.append(("recency", W_RECENCY, "no recorded airing"))
    else:
        hours = max((slot.start - last).total_seconds() / 3600.0, 0.0)
        comps.append(("recency", W_RECENCY * min(hours, RECENCY_CAP_H) / RECENCY_CAP_H,
                      f"last on {hours:.0f}h earlier"))

    if f.unplayed_episodes is not None:
        comps.append(("inventory", W_INVENTORY * min(f.unplayed_episodes, INVENTORY_CAP) / INVENTORY_CAP,
                      f"{f.unplayed_episodes} unplayed episode(s)"))
    if f.airings_7d:
        comps.append(("exposure_7d", -W_EXPOSURE_7D * f.airings_7d, f"{f.airings_7d} airing(s) in 7d"))
    if f.airings_30d:
        comps.append(("exposure_30d", -W_EXPOSURE_30D * f.airings_30d, f"{f.airings_30d} airing(s) in 30d"))
    if p.weight != 1.0:
        comps.append(("rotation_weight", W_WEIGHT * (p.weight - 1.0), f"Rotation Weight {p.weight:g}"))
    if p.wdbs_rating is not None:
        comps.append(("wdbs_rating", W_WDBS_RATING * (p.wdbs_rating - 5.0), f"WDBS Rating {p.wdbs_rating:g}"))
    iw = imdb_weighted(p.imdb_rating, p.imdb_votes)
    if iw is not None:
        comps.append(("imdb", W_IMDB * (iw - IMDB_PRIOR_MEAN),
                      f"IMDb {p.imdb_rating:g} on {p.imdb_votes or 0} votes -> {iw:.2f} weighted"))
    if p.manual_boost:
        comps.append(("manual_boost", p.manual_boost, f"Manual Boost {p.manual_boost:+g}"))
    if f.episodes_in_library is not None and f.episodes_in_library < SMALL_LIBRARY and assigned:
        comps.append(("exhaustion", -W_SMALL_LIBRARY * assigned,
                      f"only {f.episodes_in_library} episode(s); repeats likely"))
    total = round(sum(c[1] for c in comps), 4)
    return Candidate(p.tag, p.program, total, comps)


def _fmt_components(comps) -> str:
    return ", ".join(f"{name} {value:+.2f} ({why})" for name, value, why in comps if abs(value) >= 0.005)


def build_lineup(programs: list[ProgramPolicy], facts: dict[str, ProgramFacts], slots: list[Slot],
                 prior: list[tuple[dt.datetime, str]] | None = None) -> list[SlotDecision]:
    """Fill slots in order. `prior` seeds spacing/counts from already-scheduled airtime."""
    if not programs:
        raise PolicyError("No programs in rotation.")
    by_tag = {p.tag: p for p in programs}
    st = _DayState()
    for start, tag, *length in sorted(prior or []):
        st.assign(tag, start, length[0] if length else 1.0)
    decisions = []

    for slot in slots:
        st.roll_day(slot.start.date())
        failures = {p.tag: gate_failures(p, facts.get(p.tag, ProgramFacts()), slot, st) for p in programs}

        if slot.locked_tag:
            p = by_tag.get(slot.locked_tag)
            hard = [why for g, why in failures.get(slot.locked_tag, []) if g in NON_RELAXABLE]
            if p is None or hard:
                raise PolicyError(f"Locked slot {slot.start} -> {slot.locked_tag} is not schedulable: "
                                  f"{'not in rotation' if p is None else '; '.join(hard)}")
            cand = score(p, facts.get(p.tag, ProgramFacts()), slot, st)
            decisions.append(SlotDecision(
                start=slot.start.isoformat(sep=" "), hour=slot.hour, daypart=slot.daypart, tag=p.tag,
                program=p.program, score=cand.score, components=cand.components,
                reason=f"{slot.start:%a %H:%M} {slot.daypart}: {p.program} is locked "
                       f"({slot.lock_reason or 'schedule lock'})."))
            st.assign(p.tag, slot.start, slot.length_h)
            continue

        chosen = None
        for level, relaxed in enumerate(RELAX_LADDER):
            eligible = [p for p in programs
                        if all(g in relaxed for g, _ in failures[p.tag])]
            if eligible:
                chosen = (level, relaxed, eligible)
                break
        if chosen is None:
            detail = "; ".join(f"{t}: {', '.join(w for _, w in fs)}" for t, fs in failures.items())
            raise PolicyError(f"No schedulable program for {slot.start}: {detail}")

        level, relaxed, eligible = chosen
        cands = [score(p, facts.get(p.tag, ProgramFacts()), slot, st) for p in eligible]
        cands.sort(key=lambda c: (-c.score, _stable(slot.start.isoformat(), c.tag)))
        win = cands[0]
        used_relax = sorted({g for g, _ in failures[win.tag] if g in relaxed})
        rejected = {t: [w for _, w in fs] for t, fs in failures.items()
                    if fs and t not in {p.tag for p in eligible}}

        parts = [f"{slot.start:%a %H:%M} {slot.daypart}: {win.program} scored {win.score:+.2f}"
                 f" = {_fmt_components(win.components) or 'no adjustments'}."]
        if len(cands) > 1:
            parts.append("Beat " + ", ".join(f"{c.program} ({c.score:+.2f})" for c in cands[1:4]) + ".")
            if cands[1].score == win.score:
                parts.append("Tie broken by stable hash.")
        if used_relax:
            parts.append(f"Relaxed {', '.join(used_relax)} because no program passed every gate "
                         f"({'; '.join(w for g, w in failures[win.tag] if g in relaxed)}).")
        if rejected:
            parts.append("Ineligible: " + "; ".join(
                f"{by_tag[t].program} ({', '.join(ws)})" for t, ws in list(rejected.items())[:5]) + ".")

        decisions.append(SlotDecision(
            start=slot.start.isoformat(sep=" "), hour=slot.hour, daypart=slot.daypart, tag=win.tag,
            program=win.program, score=win.score, reason=" ".join(parts), relaxed=used_relax,
            components=win.components,
            runners_up=[{"tag": c.tag, "program": c.program, "score": c.score} for c in cands[1:4]],
            rejected=rejected))
        st.assign(win.tag, slot.start, slot.length_h)
    return decisions


def hourly_slots(start: dt.datetime, end: dt.datetime,
                 locks: dict[dt.datetime, tuple[str, str]] | None = None) -> list[Slot]:
    """Hour slots covering [start, end). A start inside an hour yields that hour's slot."""
    locks = locks or {}
    cur = start.replace(minute=0, second=0, microsecond=0)
    out = []
    while cur < end:
        tag, why = locks.get(cur, (None, None))
        out.append(Slot(cur, tag, why))
        cur += dt.timedelta(hours=1)
    return out


def halfhour_slots(start: dt.datetime, end: dt.datetime,
                   locks: dict[dt.datetime, tuple[str, str]] | None = None) -> list[Slot]:
    """Half-hour slots covering [start, end), so each half of an hour can carry a different show.
    A start inside a half-hour yields that half-hour's slot."""
    locks = locks or {}
    cur = start.replace(minute=0 if start.minute < 30 else 30, second=0, microsecond=0)
    out = []
    while cur < end:
        tag, why = locks.get(cur, (None, None))
        out.append(Slot(cur, tag, why, length_h=0.5))
        cur += dt.timedelta(minutes=30)
    return out
