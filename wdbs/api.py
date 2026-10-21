"""WDBS Control Plane API.

The single backend surface for the CLI, WDBS Home, an AI station manager, a
remote UI, and the future Presentation Manager. Every method returns plain
JSON-serializable dicts. Read methods never write FS42. The only FS42-writing
method is replan(apply=True), which goes through wdbs.replan's guarded path.

Governing principle: AI plans; deterministic code validates and executes; the
owner remains final programmer. Methods that change the air (replan) default to
a dry run and require an explicit apply.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import asdict
from pathlib import Path

from . import era_id as eid
from . import fs42_adapter as fs
from . import player_control
from . import replan as rp
from . import weather_break as wb
from .config import Settings
from .era import tag_eras
from .history import recent_airings
from .policy import PolicyError, build_lineup, halfhour_slots
from .programming import gather_facts, load_manifest, rotation_policies
from .state import StateStore, now_str

RECORDER_STALE = dt.timedelta(minutes=2)
MIN_HORIZON = dt.timedelta(hours=18)


def _iso(x):
    return x.isoformat(sep=" ") if isinstance(x, dt.datetime) else x


def _block(b: fs.Block | None, catalog: dict | None = None) -> dict | None:
    if b is None:
        return None
    d = {"block_id": b.id, "start": _iso(b.start), "end": _iso(b.end), "title": b.title,
         "program_tag": b.tag_path, "sequence": b.sequence_name}
    if catalog and b.content_ids and b.content_ids[0] in catalog:
        e = catalog[b.content_ids[0]]
        d.update({"catalog_id": e.id, "episode": e.episode.code, "file_path": e.path})
    return d


def _half(t: dt.datetime) -> dt.datetime:
    """Floor to the half-hour slot grid the policy engine plans on."""
    return t.replace(minute=0 if t.minute < 30 else 30, second=0, microsecond=0)


def _slot_sequences(slot: dict) -> list[tuple[str, str]]:
    """(sequence name, tag) pairs a template slot uses; a split hour has parallel lists."""
    names, tags = slot.get("sequence"), slot.get("tags")
    if isinstance(tags, list):
        names = names if isinstance(names, list) else [names] * len(tags)
        return list(zip(names, tags))
    return [(names, tags)]


class ControlPlane:
    def __init__(self, settings: Settings | None = None, store: StateStore | None = None,
                 clock=dt.datetime.now):
        self.settings = settings or Settings()
        self.store = store or StateStore(self.settings.state_db)
        self.clock = clock

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _now(self) -> dt.datetime:
        return self.clock().replace(microsecond=0)

    def _conn(self):
        return fs.connect_ro(self.settings.fs42_db)

    def stations(self) -> list[fs.Station]:
        return fs.load_stations(self.settings)

    def _station(self, channel: int) -> fs.Station:
        return fs.station_for_channel(self.settings, channel)

    def _manifest(self, channel: int):
        m = load_manifest(self.settings.manifest)
        if m.channel is not None and m.channel != channel:
            return None
        return m

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    def player(self) -> dict:
        now = self._now()
        pids = fs.player_pids()
        st = fs.read_status(self.settings)
        live = self.store.get_live()
        hb = (live.get("recorder_heartbeat") or {}).get("value")
        hb_ts = fs.parse_ts(hb) if hb else None
        return {
            "online": bool(pids),
            "pids": pids,
            "status": st.status if st else None,
            "channel": st.channel if st else None,
            "network": st.network_name if st else None,
            "status_since": _iso(st.timestamp) if st and st.timestamp else None,
            "recorder_heartbeat": hb,
            "recorder_ok": bool(hb_ts and now - hb_ts < RECORDER_STALE),
        }

    def now(self, channel: int | None = None) -> dict:
        """What is on air (from the player) and what the DB schedule says should be."""
        now = self._now()
        st = fs.read_status(self.settings)
        player = self.player()
        ch = channel if channel is not None else (st.channel if st else None)
        out = {"at": _iso(now), "player": player, "channel": ch, "on_air": None, "scheduled": None,
               "schedule_match": None}
        if ch is None:
            return out
        station = self._station(ch)
        with self._conn() as conn:
            catalog = fs.get_catalog(conn, station.network_name)
            blocks = fs.get_blocks(conn, station.network_name, now - dt.timedelta(hours=4),
                                   now + dt.timedelta(hours=4))
        sched = fs.block_at(blocks, now)
        out["scheduled"] = _block(sched, catalog)
        if st and st.channel == ch and st.file_path:
            entry = fs.catalog_by_path(catalog).get(st.file_path)
            out["on_air"] = {
                "status": st.status, "segment_type": st.content_type, "title": st.title,
                "file_path": st.file_path, "segment_started": _iso(st.timestamp),
                "segment_offset_s": st.offset_s, "segment_length_s": st.segment_s,
                "program_tag": entry.tag if entry else None,
                "episode": entry.episode.code if entry else None,
                "episode_title": entry.title if entry else None,
            }
            if sched is not None:
                paths = {p.get("path") for p in sched.plan_paths()}
                out["schedule_match"] = st.file_path in paths
        return out

    def next(self, channel: int, count: int = 3) -> dict:
        now = self._now()
        station = self._station(channel)
        with self._conn() as conn:
            catalog = fs.get_catalog(conn, station.network_name)
            blocks = fs.get_blocks(conn, station.network_name, now, now + dt.timedelta(days=3))
        upcoming = [b for b in blocks if b.start > now][:count]
        return {"channel": channel, "at": _iso(now), "next": [_block(b, catalog) for b in upcoming]}

    def schedule(self, channel: int, start: dt.datetime | None = None, hours: float = 24) -> dict:
        start = start or self._now()
        end = start + dt.timedelta(hours=hours)
        station = self._station(channel)
        with self._conn() as conn:
            catalog = fs.get_catalog(conn, station.network_name)
            blocks = fs.get_blocks(conn, station.network_name, start, end)
            first, last = fs.schedule_extent(conn, station.network_name)
        return {"channel": channel, "network": station.network_name, "from": _iso(start), "to": _iso(end),
                "schedule_start": _iso(first), "schedule_end": _iso(last),
                "blocks": [_block(b, catalog) for b in blocks]}

    def catalog(self, channel: int, check_media: bool = True) -> dict:
        station = self._station(channel)
        manifest = self._manifest(channel)
        rotation = {p["tag"]: p for p in (manifest.programs if manifest else [])}
        with self._conn() as conn:
            catalog = fs.get_catalog(conn, station.network_name)
            sequences = fs.get_sequences(conn, station.network_name)
        tags: dict[str, dict] = {}
        for e in catalog.values():
            t = tags.setdefault(e.tag, {"tag": e.tag, "entries": 0, "features": 0, "missing_on_disk": 0,
                                        "content_types": set()})
            t["entries"] += 1
            t["content_types"].add(e.content_type)
            if e.content_type == "feature":
                t["features"] += 1
            if check_media and not os.path.exists(e.path):
                t["missing_on_disk"] += 1
        for tag, t in tags.items():
            t["content_types"] = sorted(t["content_types"])
            seq = sequences.get((tag, tag))
            t["sequence"] = None if not seq else {"episodes": len(seq.episodes), "cursor": seq.current_index,
                                                  "next": os.path.basename(seq.next_path() or "") or None}
            t["in_rotation"] = tag in rotation
        for tag in rotation:
            if tag not in tags:
                tags[tag] = {"tag": tag, "entries": 0, "features": 0, "missing_on_disk": 0,
                             "content_types": [], "sequence": None, "in_rotation": True}
        ready = [t for t in tags.values() if t["in_rotation"] and t["features"] and t["sequence"]
                 and not t["missing_on_disk"]]
        return {"channel": channel, "network": station.network_name, "entries": len(catalog),
                "rotation_programs": len(rotation), "rotation_ready": len(ready),
                "missing_on_disk": sum(t["missing_on_disk"] for t in tags.values()),
                "tags": sorted(tags.values(), key=lambda t: t["tag"])}

    def history(self, channel: int, tag: str | None = None) -> dict:
        now = self._now()
        station = self._station(channel)
        manifest = self._manifest(channel)
        policies = rotation_policies(manifest, self.store, channel) if manifest else []
        if tag:
            policies = [p for p in policies if p.tag == tag]
        with self._conn() as conn:
            _facts, hist = gather_facts(conn, self.store, station, policies, now, check_media=False)
        return {"channel": channel, "at": _iso(now),
                "programs": [dict(h.as_dict(), program=p.program) for p in policies for h in [hist[p.tag]]],
                "recent_airings": recent_airings(self.store, channel, 10)}

    def status(self) -> dict:
        """The normalized WDBS state in one call."""
        now = self._now()
        out = {"at": _iso(now), "player": self.player(), "channels": [], "warnings": []}
        with self._conn() as conn:
            for s in self.stations():
                ch = {"channel": s.channel, "network": s.network_name, "type": s.network_type}
                if s.network_type == "standard":
                    first, last = fs.schedule_extent(conn, s.network_name)
                    catalog = fs.get_catalog(conn, s.network_name)
                    blocks = fs.get_blocks(conn, s.network_name, now - dt.timedelta(hours=4),
                                           now + dt.timedelta(hours=6))
                    cur = fs.block_at(blocks, now)
                    nxt = next((b for b in blocks if b.start > now), None)
                    ch.update({"catalog_entries": len(catalog), "schedule_end": _iso(last),
                               "horizon_hours": round((last - now).total_seconds() / 3600, 1) if last else None,
                               "current_block": _block(cur, catalog), "next_block": _block(nxt, catalog)})
                    if last is None or last - now < MIN_HORIZON:
                        out["warnings"].append(f"Channel {s.channel} schedule horizon is short (ends {last}).")
                out["channels"].append(ch)
        m = load_manifest(self.settings.manifest)
        out["manifest"] = {"path": str(m.path), "programs": len(m.programs), "channel": m.channel,
                           "generated_at": _iso(m.generated_at), "stale": m.stale, "error": m.error}
        if m.stale:
            out["warnings"].append("Rotation manifest is stale or unreadable.")
        if m.channel is not None:
            out["active_rotation_count"] = len([p for p in rotation_policies(m, self.store, m.channel)
                                                if p.in_rotation])
        if not out["player"]["online"]:
            out["warnings"].append("field_player.py is not running.")
        if not out["player"]["recorder_ok"]:
            out["warnings"].append("Playout recorder heartbeat is missing or stale.")
        on_air = self.now()
        out["on_air"] = on_air["on_air"]
        out["schedule_match"] = on_air["schedule_match"]
        if on_air["schedule_match"] is False:
            out["warnings"].append("Player is airing something other than the DB schedule "
                                   "(it may be running an older in-memory schedule).")
        return out

    # ------------------------------------------------------------------
    # verification
    # ------------------------------------------------------------------

    def verify(self, channel: int, check_media: bool = True) -> dict:
        now = self._now()
        station = self._station(channel)
        checks = []

        def check(name, ok, detail="", level="error"):
            checks.append({"check": name, "ok": bool(ok), "level": level, "detail": detail})

        with self._conn() as conn:
            name = station.network_name
            blocks = fs.get_blocks(conn, name)
            catalog = fs.get_catalog(conn, name)
            sequences = fs.get_sequences(conn, name)
            gaps = [(a.id, b.id) for a, b in zip(blocks, blocks[1:]) if b.start != a.end]
            check("schedule contiguous", not gaps, f"{len(blocks)} blocks, {len(gaps)} gaps/overlaps {gaps[:5]}")
            last = blocks[-1].end if blocks else None
            check("schedule horizon >= 18h", last is not None and last - now >= MIN_HORIZON,
                  f"ends {last}", level="warning")
            check("block on air now", fs.block_at(blocks, now) is not None, "")
            orphans = sorted({c for b in blocks for c in b.content_ids if c not in catalog})
            check("no orphan catalog references", not orphans, str(orphans[:10]))
            future_missing = []
            if check_media:
                for b in blocks:
                    if b.end < now:
                        continue
                    for e in b.plan_paths():
                        p = e.get("path")
                        if p and not e.get("is_stream") and not wb.is_web_entry(e) and not os.path.exists(p):
                            future_missing.append(p)
                check("media for current/future blocks present", not future_missing,
                      f"{len(future_missing)} missing {future_missing[:3]}")
            bad = []
            for key, seq in sequences.items():
                if not seq.episodes:
                    bad.append(f"{key[0]} empty")
                    continue
                mine = [b for b in blocks if b.sequence_key and (b.sequence_name, b.tag_path) == key]
                if mine and mine[-1].content_ids and mine[-1].content_ids[0] in catalog:
                    idx = seq.index_of(catalog[mine[-1].content_ids[0]].path)
                    if idx is not None and not seq.follows(idx):
                        bad.append(f"{key[0]} cursor {seq.current_index} vs schedule {idx + 1}")
            check("sequence cursors consistent with schedule", not bad, "; ".join(bad[:6]))
            for tmpl_name, tmpl in (station.raw.get("day_templates") or {}).items():
                missing = sorted({name for s in tmpl.values() if isinstance(s, dict) and s.get("sequence")
                                  for name, tag in _slot_sequences(s) if (name, tag) not in sequences})
                used = any(station.raw.get(d) == tmpl_name for d in
                           ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))
                if used:
                    check(f"template {tmpl_name} sequences exist", not missing, ", ".join(missing))
            order = []
            for key, seq in sequences.items():
                codes = [fs.parse_episode(p) for p in seq.episodes]
                pairs = [(c.season, c.episode) for c in codes if c.code]
                if len(pairs) == len(codes) and pairs != sorted(pairs):
                    order.append(key[0])
            check("sequence order matches episode numbering", not order,
                  f"out of order (mixed file naming): {', '.join(sorted(order))}", level="warning")
        m = self._manifest(channel)
        if m is not None:
            check("rotation manifest fresh", not m.stale, f"generated {m.generated_at}", level="warning")
            with self._conn() as conn:
                small = [f"{p['tag']} ({n})" for p in m.programs
                         for n in [len([e for e in catalog.values() if e.tag == p['tag'] and e.content_type == 'feature'])]
                         if n < 10]
            check("rotation programs have >= 10 episodes", not small, ", ".join(small), level="warning")
            # BUG-004: no Premiere Year and no Era Override means the 00s bug, IDs and side panels by default.
            no_era = sorted(p["program"] for p in m.programs
                            if p.get("premiere_year") is None and not p.get("era_override"))
            check("rotation programs have a Premiere Year (era dressing)", not no_era, ", ".join(no_era),
                  level="warning")
        p = self.player()
        check("player running", p["online"], str(p["pids"]), level="warning")
        check("recorder heartbeat fresh", p["recorder_ok"], str(p["recorder_heartbeat"]), level="warning")
        errors = [c for c in checks if not c["ok"] and c["level"] == "error"]
        return {"channel": channel, "at": _iso(now), "ok": not errors, "checks": checks}

    # ------------------------------------------------------------------
    # programming
    # ------------------------------------------------------------------

    def plan_lineup(self, channel: int, start: dt.datetime, end: dt.datetime,
                    prior_blocks: list[fs.Block] | None = None,
                    continuation: tuple[dt.datetime, str] | None = None,
                    eligible_tags: set[str] | None = None,
                    assume_sequences: set[str] | None = None,
                    check_media: bool = True) -> dict:
        """Run the policy engine for [start, end). Read-only.

        eligible_tags: programs outside this set are gated out (e.g. overnight catalog readiness).
        assume_sequences: tags whose FS42 sequence will be created before building (overnight scan).
        """
        now = self._now()
        station = self._station(channel)
        manifest = self._manifest(channel)
        if manifest is None or not manifest.programs:
            raise PolicyError("No rotation manifest for this channel.")
        policies = rotation_policies(manifest, self.store, channel)
        with self._conn() as conn:
            facts, _ = gather_facts(conn, self.store, station, policies, now, check_media=check_media)
            if prior_blocks is None:
                # 3 days back: the rotation penalty compares each slot with the same time on past days.
                prior_blocks = [b for b in fs.get_blocks(conn, station.network_name,
                                                         start - dt.timedelta(days=3), start)
                                if b.start < start]
        for p in policies:
            if eligible_tags is not None and p.tag not in eligible_tags and p.in_rotation:
                p.in_rotation = False
                p.inactive_reason = "not catalog-ready tonight"
            if assume_sequences and p.tag in assume_sequences:
                facts[p.tag].sequence_ready = True
        first = _half(start)
        prior = sorted({(_half(b.start), b.tag_path, (b.end - b.start).total_seconds() / 3600.0)
                        for b in prior_blocks if b.tag_path and _half(b.start) < first})
        locks = {}
        for ts, tag in self.store.slot_locks(channel, start - dt.timedelta(hours=1), end).items():
            hour = fs.parse_ts(ts)  # operator locks are per hour: hold both halves
            for half in (hour, hour + dt.timedelta(minutes=30)):
                locks[half] = (tag, "operator slot lock")
        if continuation:
            locks[continuation[0]] = (continuation[1], "half-hour already started before the cutoff")
        slots = halfhour_slots(start, end, locks)
        decisions = build_lineup(policies, facts, slots, prior=prior)
        return {"channel": channel, "from": _iso(start), "to": _iso(end),
                "manifest_generated_at": _iso(manifest.generated_at), "manifest_stale": manifest.stale,
                "programs": [{"tag": p.tag, "program": p.program, "in_rotation": p.in_rotation,
                              "inactive_reason": p.inactive_reason,
                              "facts": {k: _iso(v) for k, v in asdict(facts[p.tag]).items()}}
                             for p in policies],
                "lineup": [d.as_dict() for d in decisions]}

    def set_show_active(self, channel: int, tag: str, active: bool, note: str | None = None) -> dict:
        manifest = self._manifest(channel)
        known = {p["tag"] for p in (manifest.programs if manifest else [])}
        if tag not in known:
            raise ValueError(f"{tag!r} is not in the channel {channel} rotation manifest "
                             f"(known: {', '.join(sorted(known))}). Add it in Notion first.")
        self.store.set_override(channel, tag, active=active, note=note)
        self.store.emit("show.activated" if active else "show.deactivated", {"tag": tag, "note": note},
                        channel=channel)
        return {"channel": channel, "tag": tag, "active": active, "note": note,
                "scope": "WDBS policy engine (wdbs plan / replan --lineup engine). "
                         "The 04:30 wdbs_overnight lineup does not read local overrides yet."}

    # ------------------------------------------------------------------
    # replan
    # ------------------------------------------------------------------

    def replan(self, channel: int, cutoff: dt.datetime, *, until: dt.datetime | None = None,
               lineup: str = "template", apply: bool = False, check_media: bool = True,
               expect_digest: str | None = None, reload_player: bool = True) -> dict:
        now = self._now()
        station = self._station(channel)
        with self._conn() as conn:
            plan = rp.compute_plan(conn, station, now=now, cutoff=cutoff, until=until)
            kept = [b for b in fs.get_blocks(conn, station.network_name) if b.id not in set(plan.delete_block_ids)]
        plan.lineup_source = lineup
        if lineup == "engine":
            eff = fs.parse_ts(plan.effective_cutoff)
            hour0 = eff.replace(minute=0, second=0)
            cont = None
            if eff != _half(eff) and kept and kept[-1].tag_path:
                cont = (_half(eff), kept[-1].tag_path)
            with self._conn() as conn:
                seqs = fs.get_sequences(conn, station.network_name)
            result = self.plan_lineup(channel, eff, fs.parse_ts(plan.rebuild_until),
                                      prior_blocks=[b for b in kept if b.start >= hour0 - dt.timedelta(days=3)],
                                      continuation=cont)
            missing = sorted({d["tag"] for d in result["lineup"] if (d["tag"], d["tag"]) not in seqs})
            if missing:
                raise rp.ReplanRefused(f"Lineup uses shows with no FS42 sequence yet: {', '.join(missing)}. "
                                       "Let the overnight job ingest them first.")
            plan.lineup = result["lineup"]
            plan.date_overrides = rp.date_overrides_from_lineup(result["lineup"])
            if result["manifest_stale"]:
                plan.warnings.append("Rotation manifest is stale; lineup uses the last exported Notion state.")
        elif lineup != "template":
            raise ValueError("lineup must be 'template' or 'engine'")

        digest = plan.digest()
        report = {"plan": plan.as_dict(), "digest": digest, "mode": "apply" if apply else "dry-run"}
        if apply:
            if expect_digest != digest:  # strict: apply always needs the digest of a reviewed dry run
                raise rp.ReplanRefused(f"Plan changed since review (digest {digest} != {expect_digest}). "
                                       "Re-run the dry run and review again.")
            result = rp.apply(self.settings, plan, now, check_media=check_media)
            if result.get("ok") and reload_player:
                rl = player_control.request_reload(self.settings, station=station.network_name)
                report["player_reload"] = rl
                if rl.get("ok"):
                    report["player_note"] = ("Player reloaded and confirmed the new schedule; the program on air "
                                             "continues and the new blocks take over at the cutoff.")
                else:
                    report["player_note"] = (f"DB updated and verified, but the player reload was not confirmed "
                                             f"({rl.get('error') or rl.get('checks')}). The new blocks air after the "
                                             "player reloads (restart, or its next schedule panic).")
                self.store.emit("player.reload", {"ok": rl.get("ok"), "error": rl.get("error"),
                                                  "request_id": rl.get("request_id")}, channel=channel)
        else:
            result = rp.rehearse(self.settings, plan, check_media=check_media)
        report["result"] = result
        self.store.record_replan(
            channel=channel, requested_cutoff=cutoff, effective_cutoff=plan.effective_cutoff,
            mode=report["mode"], lineup_source=lineup, backup_path=result.get("backup"),
            blocks_deleted=len(plan.delete_block_ids) if result.get("ok") else 0,
            blocks_inserted=len(result.get("new_blocks", [])) if result.get("ok") else 0,
            ok=bool(result.get("ok")), report={"digest": digest, "checks": result.get("checks"),
                                               "error": result.get("error")})
        if apply:
            self.store.emit("replan.applied" if result.get("ok") else "replan.failed",
                            {"digest": digest, "cutoff": plan.effective_cutoff, "backup": result.get("backup")},
                            channel=channel)
        return report

    def weather_break(self, channel: int, at: dt.datetime | None, *, url: str, break_number: int = 1,
                      target_s: float = 60.0, apply: bool = False, expect_digest: str | None = None,
                      reload_player: bool = True, intro_path: str | None = None,
                      morning_intro_path: str | None = None, asap: bool = False, auto: bool = False) -> dict:
        """Swap commercials in one future break for a timed WeatherStar segment (dry run by default)."""
        now = self._now()
        station = self._station(channel)
        intro = wb.load_intro(intro_path) if intro_path else None
        morning = wb.load_intro(morning_intro_path) if morning_intro_path else None
        with self._conn() as conn:
            if asap:
                plan = wb.compute_asap(conn, station, now=now, url=url, target_s=target_s,
                                       intro=intro, morning_intro=morning)
            else:
                plan = wb.compute_plan(conn, station, now=now, at=at, url=url, break_number=break_number,
                                       target_s=target_s, intro=intro, morning_intro=morning)
        digest = plan.digest()
        report = {"plan": plan.as_dict(), "digest": digest, "mode": "auto" if auto else "apply" if apply else "dry-run"}
        if not (apply or auto):
            report["result"] = wb.rehearse(self.settings, plan)
            return report
        if auto:
            rehearsal = wb.rehearse(self.settings, plan)
            if not rehearsal["ok"]:
                report["result"] = {**rehearsal, "note": "rehearsal failed; live DB not touched"}
                return report
        elif expect_digest != digest:
            raise wb.WeatherBreakRefused(f"Plan changed since review (digest {digest} != {expect_digest}). "
                                         "Re-run the dry run and review again.")
        result = wb.apply(self.settings, plan, now)
        report["result"] = result
        self.store.emit("weather_break.applied" if result.get("ok") else "weather_break.failed",
                        {"block_id": plan.block_id, "airs_at": plan.airs_at, "segment_s": plan.segment_s,
                         "sidecar": result.get("sidecar"), "error": result.get("error")}, channel=channel)
        if result.get("ok") and reload_player:
            report["player_reload"] = self._reload_after_write(channel, station.network_name)
        return report

    def weather_hourly(self, channel: int, *, url: str, target_s: float = 60.0, until: dt.datetime | None = None,
                       apply: bool = False, auto: bool = False, expect_digest: str | None = None,
                       reload_player: bool = True, intro_path: str | None = None,
                       upgrade_intro: bool = False, at: dt.datetime | None = None,
                       morning_intro_path: str | None = None) -> dict:
        """Hourly rule: one weather break per clock hour (dry run by default; auto = rehearse then apply)."""
        now = self._now()
        station = self._station(channel)
        intro = wb.load_intro(intro_path) if intro_path else None
        morning = wb.load_intro(morning_intro_path) if morning_intro_path else None
        with self._conn() as conn:
            if upgrade_intro:
                if intro is None:
                    raise wb.WeatherBreakRefused("--upgrade-intro needs --intro <file>.")
                plans, skipped = wb.compute_intro_upgrade(conn, station, now=now, intro=intro, at=at, url=url,
                                                          morning_intro=morning), []
            else:
                plans, skipped = wb.compute_hourly(conn, station, now=now, url=url, target_s=target_s,
                                                   until=until, intro=intro, morning_intro=morning)
        digest = wb.batch_digest(plans)
        report = {"plans": [p.as_dict() for p in plans], "skipped": skipped, "digest": digest,
                  "mode": "auto" if auto else "apply" if apply else "dry-run"}
        if not plans:
            report["result"] = {"ok": True, "checks": [], "note": "nothing to do: every weather break in range is already "
                                "up to date" if upgrade_intro else "every hour in range already has a weather break"}
            return report
        rehearsal = wb.rehearse(self.settings, plans)
        if not (apply or auto):
            report["result"] = rehearsal
            return report
        if auto and not rehearsal["ok"]:
            report["result"] = {**rehearsal, "note": "rehearsal failed; live DB not touched"}
            self.store.emit("weather_break.failed", {"mode": "auto", "stage": "rehearsal", "digest": digest},
                            channel=channel)
            return report
        if apply and expect_digest != digest:
            raise wb.WeatherBreakRefused(f"Plan changed since review (digest {digest} != {expect_digest}). "
                                         "Re-run the dry run and review again.")
        result = wb.apply(self.settings, plans, now)
        report["result"] = result
        self.store.emit("weather_break.applied" if result.get("ok") else "weather_break.failed",
                        {"mode": report["mode"], "blocks": [p.block_id for p in plans],
                         "airs_at": [p.airs_at for p in plans], "sidecar": result.get("sidecar"),
                         "error": result.get("error")}, channel=channel)
        if result.get("ok") and reload_player:
            report["player_reload"] = self._reload_after_write(channel, station.network_name)
        return report

    def weather_break_revert(self, channel: int, sidecar: Path, *, reload_player: bool = True) -> dict:
        station = self._station(channel)
        result = wb.revert(self.settings, sidecar, self._now())
        report = {"mode": "revert", "result": result}
        self.store.emit("weather_break.reverted" if result.get("ok") else "weather_break.revert_failed",
                        {"sidecar": str(sidecar), "error": result.get("error")}, channel=channel)
        if result.get("ok") and reload_player:
            report["player_reload"] = self._reload_after_write(channel, station.network_name)
        return report

    def era_ids(self, channel: int, *, apply: bool = False, auto: bool = False, expect_digest: str | None = None,
               reload_player: bool = True, assets_dir: str | None = None, at: dt.datetime | None = None,
               off: bool = False) -> dict:
        """Swap station-ID bumps in future blocks for each rotation show's era ID (dry run by default).

        off=True is the off switch: put the generic station ID back wherever an era ID is scheduled,
        without reading the rotation manifest (so it still works if the manifest is broken)."""
        now = self._now()
        station = self._station(channel)
        assets = str(assets_dir) if assets_dir else f"{self.settings.fs42_root.as_posix()}/{eid.ERA_ASSET_DIR}"
        if off:
            with self._conn() as conn:
                bumps = [e.path for e in fs.get_catalog(conn, station.network_name).values() if e.content_type == "bump"]
            if len(bumps) != 1:
                raise eid.EraIdRefused(f"--off needs exactly one station-ID bump in the {station.network_name} "
                                       f"catalog; found {len(bumps)}.")
            generic = wb.load_intro(bumps[0])
            with self._conn() as conn:
                plans, skipped = eid.compute_off(conn, station, now=now, generic=generic, assets_dir=assets, at=at)
        else:
            m = load_manifest(self.settings.manifest)
            if m.error:
                raise eid.EraIdRefused(f"Rotation manifest unreadable ({m.error}); era IDs left as they are.")
            if m.channel is not None and m.channel != channel:
                raise eid.EraIdRefused(f"Rotation manifest is for channel {m.channel}, not {channel}.")
            ids = eid.load_ids(assets)
            with self._conn() as conn:
                plans, skipped = eid.compute(conn, station, now=now, eras=tag_eras(m.programs), ids=ids,
                                             assets_dir=assets, at=at)
        digest = wb.batch_digest(plans)
        report = {"plans": [p.as_dict() for p in plans], "skipped": skipped, "digest": digest,
                  "mode": "auto" if auto else "apply" if apply else "dry-run", "off": off}
        if not plans:
            report["result"] = {"ok": True, "checks": [], "note": "nothing to do: no era IDs scheduled in range"
                                if off else "nothing to do: every station ID in range is already its show's era ID"}
            return report
        rehearsal = eid.rehearse(self.settings, plans)
        if not (apply or auto):
            report["result"] = rehearsal
            return report
        if auto and not rehearsal["ok"]:
            report["result"] = {**rehearsal, "note": "rehearsal failed; live DB not touched"}
            self.store.emit("era_id.failed", {"mode": "auto", "stage": "rehearsal", "digest": digest, "off": off},
                            channel=channel)
            return report
        if apply and expect_digest != digest:
            raise eid.EraIdRefused(f"Plan changed since review (digest {digest} != {expect_digest}). "
                                   "Re-run the dry run and review again.")
        result = eid.apply(self.settings, plans, now)
        report["result"] = result
        self.store.emit("era_id.applied" if result.get("ok") else "era_id.failed",
                        {"mode": report["mode"], "blocks": [p.block_id for p in plans],
                         "ids": sum(len(p.swapped) for p in plans), "sidecar": result.get("sidecar"),
                         "error": result.get("error"), "off": off}, channel=channel)
        if result.get("ok") and reload_player:
            report["player_reload"] = self._reload_after_write(channel, station.network_name)
        return report

    def era_id_revert(self, channel: int, sidecar: Path, *, reload_player: bool = True) -> dict:
        station = self._station(channel)
        result = wb.revert(self.settings, sidecar, self._now())
        report = {"mode": "revert", "result": result}
        self.store.emit("era_id.reverted" if result.get("ok") else "era_id.revert_failed",
                        {"sidecar": str(sidecar), "error": result.get("error")}, channel=channel)
        if result.get("ok") and reload_player:
            report["player_reload"] = self._reload_after_write(channel, station.network_name)
        return report

    def _reload_after_write(self, channel: int, station: str) -> dict:
        rl = player_control.request_reload(self.settings, station=station)
        self.store.emit("player.reload", {"ok": rl.get("ok"), "error": rl.get("error"),
                                          "request_id": rl.get("request_id")}, channel=channel)
        return rl

    def reload_player(self, channel: int | None = None) -> dict:
        station = self._station(channel).network_name if channel is not None else None
        r = player_control.request_reload(self.settings, station=station)
        self.store.emit("player.reload", {"ok": r.get("ok"), "error": r.get("error"),
                                          "request_id": r.get("request_id"), "manual": True}, channel=channel)
        return r

    def fs42_patch_status(self) -> dict:
        # The FS42 patch tool is not part of the public export (license review pending), so report that
        # plainly instead of a traceback. When the module is present this behaves exactly as before.
        import importlib.util
        if importlib.util.find_spec(f"{__package__}.fs42_patch") is None:
            return {"supported": False,
                    "error": "wdbs.fs42_patch is not included in this export; FS42 patch status is unavailable"}
        from . import fs42_patch
        return {name: fs42_patch.status(self.settings.fs42_root, name) for name in fs42_patch.PATCHES}

    def events(self, kind: str | None = None, limit: int = 30) -> dict:
        return {"events": self.store.events(kind, limit)}
