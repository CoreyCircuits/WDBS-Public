"""wdbs - operator CLI. Formatting only; all logic lives in wdbs.api."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

from . import fs42_adapter as fs
from .api import ControlPlane
from .policy import PolicyError
from .replan import ReplanRefused
from .timeparse import parse_when
from .weather_break import WeatherBreakRefused

DEFAULT_CHANNEL = 10


def _dump(obj):
    print(json.dumps(obj, indent=2, default=str))


def _hm(ts: str | None) -> str:
    return ts[5:16] if ts else "-"


def _prog(b: dict | None) -> str:
    if not b:
        return "(nothing scheduled)"
    ep = f" [{b['episode']}]" if b.get("episode") else ""
    return f"{b.get('program_tag') or '?'}{ep}  {b['title']}"


def cmd_status(cp: ControlPlane, a):
    s = cp.status()
    if a.json:
        return _dump(s)
    p = s["player"]
    print(f"WDBS status @ {s['at']}")
    print(f"  Player    {'ONLINE' if p['online'] else 'OFFLINE'} pids={p['pids']} status={p['status']} "
          f"channel={p['channel']}  recorder={'ok' if p['recorder_ok'] else 'STALE'}")
    oa = s.get("on_air")
    if oa:
        print(f"  On air    {oa.get('program_tag') or '?'} {oa.get('episode') or ''} ({oa['segment_type']}) "
              f"{'= schedule' if s.get('schedule_match') else '!= DB schedule' if s.get('schedule_match') is False else ''}")
    for ch in s["channels"]:
        if ch["type"] != "standard":
            print(f"  Ch {ch['channel']:>3} {ch['network']} ({ch['type']})")
            continue
        print(f"  Ch {ch['channel']:>3} {ch['network']}: catalog={ch['catalog_entries']} "
              f"horizon={ch['horizon_hours']}h (to {ch['schedule_end']})")
        print(f"         now:  {_prog(ch['current_block'])}")
        print(f"         next: {_hm(ch['next_block']['start']) if ch['next_block'] else ''} {_prog(ch['next_block'])}")
    m = s["manifest"]
    print(f"  Manifest  {m['programs']} programs, generated {m['generated_at']}{' (STALE)' if m['stale'] else ''}; "
          f"active rotation {s.get('active_rotation_count', '?')}")
    for w in s["warnings"]:
        print(f"  ! {w}")


def cmd_now(cp, a):
    r = cp.now(a.channel)
    if a.json:
        return _dump(r)
    oa, sc = r["on_air"], r["scheduled"]
    print(f"Channel {r['channel']} @ {r['at']}")
    if oa:
        print(f"  On air:    {oa.get('program_tag') or '?'} {oa.get('episode') or ''} {oa.get('episode_title') or oa['title']}"
              f"  [{oa['segment_type']} segment since {oa['segment_started']}]")
    else:
        print("  On air:    (player not on this channel or no status)")
    print(f"  Scheduled: {_hm(sc['start']) if sc else ''}-{_hm(sc['end'])[6:] if sc else ''} {_prog(sc)}")
    if r["schedule_match"] is False:
        print("  ! Player output differs from the DB schedule (player may hold an older in-memory schedule).")


def cmd_next(cp, a):
    r = cp.next(a.channel, a.count)
    if a.json:
        return _dump(r)
    for b in r["next"]:
        print(f"  {_hm(b['start'])}  {_prog(b)}")


def cmd_schedule(cp, a):
    start = parse_when(a.start, dt.datetime.now()) if a.start else None
    r = cp.schedule(a.channel, start, a.hours)
    if a.json:
        return _dump(r)
    print(f"Channel {r['channel']} {r['network']}  (schedule runs {r['schedule_start']} -> {r['schedule_end']})")
    for b in r["blocks"]:
        print(f"  {_hm(b['start'])}-{b['end'][11:16]}  {_prog(b)}")


def cmd_channel(cp, a):
    cat = cp.catalog(a.channel, check_media=False)
    hist = cp.history(a.channel)
    if a.json:
        return _dump({"catalog": cat, "history": hist})
    print(f"Channel {cat['channel']} {cat['network']}: {cat['rotation_ready']}/{cat['rotation_programs']} rotation programs ready")
    print(f"  {'program':<24}{'last aired':<18}{'7d':>4}{'30d':>5}{'uniq':>6}{'unplayed':>9}  next")
    for p in hist["programs"]:
        print(f"  {p['program'][:23]:<24}{(p['last_aired'] or '-')[5:16]:<18}{p['airings_7d']:>4}{p['airings_30d']:>5}"
              f"{p['unique_episodes_aired']:>6}{p['unplayed_episodes']:>9}  {(p['next_scheduled_at'] or '')[5:16]} "
              f"{(p['next_episode'] or '')[:40]}")


def cmd_catalog(cp, a):
    r = cp.catalog(a.channel, check_media=not a.no_media_check)
    if a.json:
        return _dump(r)
    print(f"Channel {r['channel']} catalog: {r['entries']} entries, rotation ready "
          f"{r['rotation_ready']}/{r['rotation_programs']}, missing on disk {r['missing_on_disk']}")
    for t in r["tags"]:
        seq = t["sequence"]
        print(f"  {'*' if t['in_rotation'] else ' '} {t['tag']:<24}{t['features']:>4} features "
              f"{t['entries']:>4} entries  missing={t['missing_on_disk']}  "
              f"seq={'-' if not seq else str(seq['cursor']) + '/' + str(seq['episodes'])}")


def cmd_verify(cp, a):
    r = cp.verify(a.channel, check_media=not a.no_media_check)
    if a.json:
        _dump(r)
    else:
        print(f"Verify channel {r['channel']} @ {r['at']}: {'OK' if r['ok'] else 'FAIL'}")
        for c in r["checks"]:
            mark = "ok  " if c["ok"] else ("FAIL" if c["level"] == "error" else "warn")
            print(f"  [{mark}] {c['check']}{': ' + c['detail'] if c['detail'] and not c['ok'] else ''}")
    return 0 if r["ok"] else 1


def cmd_plan(cp, a):
    now = dt.datetime.now()
    start = parse_when(a.start, now)
    end = start + dt.timedelta(hours=a.hours)
    r = cp.plan_lineup(a.channel, start, end, check_media=a.media_check)
    if a.json:
        return _dump(r)
    for d in r["lineup"]:
        print(f"{d['start'][5:16]}  {d['program']:<22} {d['score']:+.2f}")
        if a.why:
            print(f"      {d['reason']}")


def _print_replan(r):
    plan = r["plan"]
    print(f"Replan channel {plan['channel']} ({r['mode']}, lineup={plan['lineup_source']}, digest {r['digest']})")
    print(f"  requested cutoff {plan['requested_cutoff']} -> effective {plan['effective_cutoff']} ({plan['cutoff_reason']})")
    if plan["current_block"]:
        print(f"  on air now (kept): {plan['current_block']['title']} until {plan['current_block']['end']}")
    print(f"  rebuild until {plan['rebuild_until']} (horizon was {plan['horizon_end']})")
    print(f"  delete {len(plan['delete_block_ids'])} future block(s); keep {plan['kept_blocks']}")
    for rb in plan["sequence_rollbacks"]:
        print(f"  rewind {rb['sequence_name']}: cursor {rb['cursor_now']} -> {rb['restore_to']} "
              f"({rb['restore_path'].rsplit('/', 1)[-1]})")
    print(f"  give back play counts on {len(plan['count_restores'])} catalog row(s)")
    for w in plan["warnings"]:
        print(f"  ! {w}")
    res = r["result"]
    if res.get("error"):
        print(f"  ERROR: {res['error']}")
    for w in res.get("build_warnings", []):
        print(f"  ! {w}")
    for c in res.get("checks", []):
        print(f"  [{'ok  ' if c['ok'] else 'FAIL'}] {c['check']}{': ' + c['detail'] if c['detail'] else ''}")
    if res.get("new_blocks"):
        print("  New schedule:")
        for b in res["new_blocks"]:
            print(f"    {b['start'][5:16]}  {b['tag'] or '?':<22} {b['title']}")
    if res.get("backup"):
        print(f"  backup: {res['backup']}{'  (RESTORED - no changes kept)' if res.get('restored') else ''}")
    print(f"  RESULT: {'OK' if res.get('ok') else 'FAILED'}")
    rl = r.get("player_reload")
    if rl:
        print(f"  player reload: {'CONFIRMED' if rl.get('ok') else 'not confirmed'}"
              f"{' - ' + rl['error'] if rl.get('error') else ''}")
    if r.get("player_note") and res.get("ok"):
        print(f"  note: {r['player_note']}")
    if r["mode"] == "dry-run" and res.get("ok"):
        print(f"  To apply exactly this plan: add --apply --expect {r['digest']}")


def cmd_replan(cp, a):
    now = dt.datetime.now()
    cutoff = parse_when(a.from_, now)
    until = parse_when(a.until, now) if a.until else None
    if a.apply and not a.expect:
        print("Refusing --apply without --expect <digest> from a reviewed dry run.", file=sys.stderr)
        return 2
    r = cp.replan(a.channel, cutoff, until=until, lineup=a.lineup, apply=a.apply,
                  check_media=not a.no_media_check, expect_digest=a.expect, reload_player=not a.no_reload)
    if a.json:
        _dump(r)
    else:
        _print_replan(r)
    return 0 if r["result"].get("ok") else 1


def cmd_show(cp, a, active: bool):
    r = cp.set_show_active(a.channel, a.tag, active, a.note)
    if a.json:
        return _dump(r)
    print(f"{a.tag} {'activated' if active else 'deactivated'} on channel {a.channel}. Scope: {r['scope']}")


def cmd_history(cp, a):
    r = cp.history(a.channel, a.tag)
    if a.json:
        return _dump(r)
    for p in r["programs"]:
        print(json.dumps(p, indent=2))
    print("Recent airings:")
    for x in r["recent_airings"]:
        print(f"  {x['started_at'][5:16]} {x['tag'] or '?':<22} {x['title']}  pos={x['position_s'] or 0:.0f}s "
              f"{'complete' if x['completed'] else ''} {x['end_reason'] or 'on air'}")


def cmd_events(cp, a):
    r = cp.events(a.kind, a.limit)
    if a.json:
        return _dump(r)
    for e in r["events"]:
        print(f"  {e['ts']} {e['kind']:<18} ch={e['channel']} {json.dumps(e['payload'])}")


def _weather_url(cp) -> str:
    for s in fs.load_stations(cp.settings):
        if s.network_type == "web" and "weather" in s.network_name.lower():
            return s.raw["web_url"]
    raise SystemExit("No weather web channel found; pass --url.")


def cmd_weather_break(cp, a):
    if a.revert:
        r = cp.weather_break_revert(a.channel, Path(a.revert), reload_player=not a.no_reload)
    elif a.hourly or a.upgrade_intro:
        if a.apply and not a.expect:
            print("Refusing --apply without --expect <digest> from a reviewed dry run.", file=sys.stderr)
            return 2
        until = parse_when(a.until, dt.datetime.now()) if a.until else None
        at = parse_when(a.at, dt.datetime.now()) if (a.upgrade_intro and a.at) else None
        r = cp.weather_hourly(a.channel, url=a.url or _weather_url(cp), target_s=a.seconds, until=until,
                              apply=a.apply, auto=a.auto, expect_digest=a.expect, reload_player=not a.no_reload,
                              intro_path=a.intro, upgrade_intro=a.upgrade_intro, at=at,
                              morning_intro_path=a.morning_intro)
    else:
        if not (a.at or a.asap):
            print("--at or --asap is required (or --hourly / --revert).", file=sys.stderr)
            return 2
        if a.apply and not a.expect:
            print("Refusing --apply without --expect <digest> from a reviewed dry run.", file=sys.stderr)
            return 2
        r = cp.weather_break(a.channel, parse_when(a.at, dt.datetime.now()) if a.at else None,
                             url=a.url or _weather_url(cp), break_number=a.break_number, target_s=a.seconds,
                             apply=a.apply, expect_digest=a.expect, reload_player=not a.no_reload,
                             intro_path=a.intro, morning_intro_path=a.morning_intro, asap=a.asap, auto=a.auto)
    if a.json:
        _dump(r)
        return 0 if r["result"].get("ok") else 1
    if "plans" in r:
        what = "intro upgrades" if a.upgrade_intro else "hourly weather breaks"
        print(f"{what.capitalize()} ({r['mode']}): {len(r['plans'])} to write, digest {r['digest']}")
        for x in r["plans"]:
            print(f"  {x['airs_at'][5:16]}  {x['segment_s']:5.1f}s  block {x['block_id']} break {x['break_number']}  "
                  f"replaces {', '.join(y['file'][:28] for y in x['replaced'])}")
        for x in r["skipped"]:
            print(f"  {x['hour'][5:]}  -- none: {x['reason']}")
    pl = r.get("plan")
    if pl:
        print(f"Weather break ({r['mode']}) on {pl['station']}: block {pl['block_id']} '{pl['block_title']}' "
              f"{pl['block_start'][11:16]}-{pl['block_end'][11:16]}")
        print(f"  break {pl['break_number']}: WeatherStar {pl['segment_s']:.1f}s airs at {pl['airs_at']}")
        for x in pl["replaced"]:
            print(f"    replaces {x['duration']:6.1f}s  {x['file']}")
        print(f"  digest {r['digest']}")
    res = r["result"]
    for c in res.get("checks", []):
        print(f"  [{'ok  ' if c['ok'] else 'FAIL'}] {c['check']}  {c['detail']}")
    if res.get("error"):
        print(f"  error: {res['error']}")
    if res.get("note"):
        print(f"  note: {res['note']}")
    for k in ("backup", "sidecar"):
        if res.get(k):
            print(f"  {k}: {res[k]}")
    if res.get("restored"):
        print("  live DB restored from backup")
    rl = r.get("player_reload")
    if rl:
        print(f"  player reload {'CONFIRMED' if rl.get('ok') else 'NOT confirmed: ' + str(rl.get('error') or rl.get('checks'))}")
    print(f"Result: {'OK' if res.get('ok') else 'FAILED'}")
    return 0 if res.get("ok") else 1


def cmd_era_id(cp, a):
    if a.revert:
        r = cp.era_id_revert(a.channel, Path(a.revert), reload_player=not a.no_reload)
    else:
        if a.apply and not a.expect:
            print("Refusing --apply without --expect <digest> from a reviewed dry run.", file=sys.stderr)
            return 2
        r = cp.era_ids(a.channel, apply=a.apply, auto=a.auto, expect_digest=a.expect, reload_player=not a.no_reload,
                       assets_dir=a.assets, at=parse_when(a.at, dt.datetime.now()) if a.at else None, off=a.off)
    if a.json:
        _dump(r)
        return 0 if r["result"].get("ok") else 1
    if "plans" in r:
        print(f"Era IDs{' OFF' if r.get('off') else ''} ({r['mode']}): {len(r['plans'])} block(s) to write, digest {r['digest']}")
        for x in r["plans"]:
            print(f"  {x['block_start'][5:16]}  {x['era']:>3}  {len(x['swapped'])} ID(s)  "
                  f"block {x['block_id']}  {x['block_title'][:40]}")
        for x in r["skipped"]:
            print(f"  {x['block_start'][5:16]}  -- left alone: {x['reason']}")
    res = r["result"]
    for c in res.get("checks", []):
        print(f"  [{'ok  ' if c['ok'] else 'FAIL'}] {c['check']}  {c['detail']}")
    for k in ("error", "note", "backup", "sidecar"):
        if res.get(k):
            print(f"  {k}: {res[k]}")
    if res.get("restored"):
        print("  live DB restored from backup")
    rl = r.get("player_reload")
    if rl:
        print(f"  player reload {'CONFIRMED' if rl.get('ok') else 'NOT confirmed: ' + str(rl.get('error') or rl.get('checks'))}")
    print(f"Result: {'OK' if res.get('ok') else 'FAILED'}")
    return 0 if res.get("ok") else 1


def cmd_player(cp, a):
    r = cp.reload_player(a.channel) if a.action == "reload" else cp.fs42_patch_status()
    if a.json or a.action != "reload":
        _dump(r)
        if r.get("supported") is False:   # patch tool withheld from the public export (see api.fs42_patch_status)
            return 1
    else:
        print(f"Reload {'CONFIRMED' if r.get('ok') else 'NOT confirmed'} (request {r['request_id']})")
        if r.get("error"):
            print(f"  {r['error']}")
        for k, v in (r.get("checks") or {}).items():
            print(f"  [{'ok  ' if v else 'FAIL'}] {k}")
        if r.get("ack"):
            print(f"  player reload took {r['ack'].get('duration_s')}s at {r['ack'].get('reloaded_at')}")
    return 0 if a.action != "reload" or r.get("ok") else 1


def cmd_prefetch(cp, a):
    from .prefetch import Prefetcher
    pf = Prefetcher(cp.settings, cp.store, max_mbps=a.max_mbps)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.action == "once":
        return _dump(pf.run_once())
    pf.run(interval=a.interval)


def cmd_recorder(cp, a):
    from .recorder import Recorder
    rec = Recorder(cp.settings, cp.store)
    if a.action == "poll":
        return _dump(rec.poll())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    rec.run(interval=a.interval)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="wdbs", description="WDBS Broadcast OS control plane")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def ch(p):
        p.add_argument("--channel", "-c", type=int, default=DEFAULT_CHANNEL)
        return p

    sub.add_parser("status", help="normalized WDBS state")
    p = sub.add_parser("now", help="what is on air")
    p.add_argument("--channel", "-c", type=int, default=None)
    p = ch(sub.add_parser("next", help="upcoming blocks"))
    p.add_argument("--count", "-n", type=int, default=3)
    p = sub.add_parser("channel", help="channel overview with playout history")
    p.add_argument("channel", type=int, nargs="?", default=DEFAULT_CHANNEL)
    p = ch(sub.add_parser("catalog", help="catalog health by show tag"))
    p.add_argument("--no-media-check", action="store_true")
    p = sub.add_parser("schedule", help="show the FS42 schedule")
    p.add_argument("channel", type=int, nargs="?", default=DEFAULT_CHANNEL)
    p.add_argument("--start", help="e.g. 'tomorrow 06:00' (default now)")
    p.add_argument("--hours", type=float, default=24)
    p = ch(sub.add_parser("verify", help="read-only health and invariant checks"))
    p.add_argument("--no-media-check", action="store_true")
    p = ch(sub.add_parser("plan", help="policy-engine lineup preview (read-only)"))
    p.add_argument("--start", default="tomorrow 00:00")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--why", action="store_true", help="print the Algorithm Reason for each slot")
    p.add_argument("--media-check", action="store_true", help="stat every cataloged file (slow over CIFS)")
    p = ch(sub.add_parser("replan", help="regenerate future schedule blocks (dry run by default)"))
    p.add_argument("--from", dest="from_", required=True, help="cutoff, e.g. 'tomorrow 06:00'")
    p.add_argument("--until", help="rebuild end (default: current horizon)")
    p.add_argument("--lineup", choices=["template", "engine"], default="template",
                   help="template = station config as-is; engine = WDBS policy engine")
    p.add_argument("--apply", action="store_true", help="write to the live FS42 DB")
    p.add_argument("--expect", help="digest from the reviewed dry run (required with --apply)")
    p.add_argument("--no-media-check", action="store_true")
    p.add_argument("--no-reload", action="store_true", help="do not ask the player to reload after apply")
    for name in ("activate-show", "deactivate-show"):
        p = ch(sub.add_parser(name, help=f"{name.split('-')[0]} a show locally (policy engine override)"))
        p.add_argument("tag")
        p.add_argument("--note")
    p = ch(sub.add_parser("history", help="playout history metrics"))
    p.add_argument("tag", nargs="?")
    p = sub.add_parser("events", help="event log")
    p.add_argument("--kind")
    p.add_argument("--limit", type=int, default=30)
    p = ch(sub.add_parser("weather-break", help="put a timed WeatherStar segment into one future break (dry run by default)"))
    p.add_argument("--at", help="a time inside the target block, e.g. 'today 21:00'")
    p.add_argument("--break", dest="break_number", type=int, default=1, help="which break in the block (default 1)")
    p.add_argument("--seconds", type=float, default=60.0, help="target segment length (default 60)")
    p.add_argument("--url", help="web URL (default: the weather channel's web_url)")
    p.add_argument("--apply", action="store_true", help="write to the live FS42 DB")
    p.add_argument("--expect", help="digest from the reviewed dry run (required with --apply)")
    p.add_argument("--revert", metavar="SIDECAR", help="put the commercials back using the .json sidecar from an apply")
    p.add_argument("--hourly", action="store_true", help="one break per clock hour (nearest :08) up to the horizon")
    p.add_argument("--until", help="with --hourly: stop at this time instead of the schedule horizon")
    p.add_argument("--auto", action="store_true", help="rehearse, then apply only if every check passes (timers and alerts)")
    p.add_argument("--intro", metavar="MP4", help="lead each weather segment with this clip (block timing unchanged)")
    p.add_argument("--morning-intro", metavar="MP4", help="lead breaks airing 05:00-10:59 with this clip instead of --intro")
    p.add_argument("--asap", action="store_true", help="the soonest break without weather (e.g. for a severe-weather alert)")
    p.add_argument("--upgrade-intro", action="store_true",
                   help="add --intro in front of weather breaks already scheduled (all future ones, or the block at --at)")
    p.add_argument("--no-reload", action="store_true", help="do not ask the player to reload after writing")
    p = ch(sub.add_parser("era-id", help="swap station IDs in future blocks for each show's era ID (dry run by default)"))
    p.add_argument("--at", help="only the block at this time, e.g. 'today 21:00' (one-off test)")
    p.add_argument("--assets", help="era ID dir (default <fs42>/wdbs_assets/era)")
    p.add_argument("--apply", action="store_true", help="write to the live FS42 DB")
    p.add_argument("--expect", help="digest from the reviewed dry run (required with --apply)")
    p.add_argument("--auto", action="store_true", help="rehearse, then apply only if every check passes (timer)")
    p.add_argument("--revert", metavar="SIDECAR", help="put the generic station IDs back using the .json sidecar")
    p.add_argument("--off", action="store_true",
                   help="put the generic station ID back wherever an era ID is scheduled (the off switch)")
    p.add_argument("--no-reload", action="store_true", help="do not ask the player to reload after writing")
    p = sub.add_parser("player", help="live player control (schedule reload only)")
    p.add_argument("action", choices=["reload", "patch-status"])
    p.add_argument("--channel", "-c", type=int, default=DEFAULT_CHANNEL)
    p = sub.add_parser("prefetch", help="warm upcoming media before air (read-only)")
    p.add_argument("action", choices=["run", "once"])
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--max-mbps", type=float, default=4.0)
    p = sub.add_parser("recorder", help="playout recorder")
    p.add_argument("action", choices=["run", "poll"])
    p.add_argument("--interval", type=float, default=5.0)
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "overnight":
        from .overnight import main as overnight_main
        return overnight_main(argv[1:])
    if argv and argv[0] == "migrate-paths":
        # wdbs.migrate_paths is not part of the public export (it handles production library data).
        import importlib.util
        if importlib.util.find_spec(f"{__package__}.migrate_paths") is None:
            print("migrate-paths is not included in this export", file=sys.stderr)
            return 2
        from .migrate_paths import main as migrate_main
        return migrate_main(argv[1:])
    if argv and argv[0] == "router":
        from .router_status import main as router_main
        return router_main(argv[1:])
    if argv and argv[0] == "playout-stats":
        from .playout_stats import main as stats_main
        return stats_main(argv[1:])
    if argv and argv[0] == "overnight-rehearse":
        from .overnight_rehearsal import main as rehearse_main
        return rehearse_main(argv[1:])
    a = build_parser().parse_args(argv)
    cp = ControlPlane()
    handlers = {
        "status": cmd_status, "now": cmd_now, "next": cmd_next, "schedule": cmd_schedule,
        "channel": cmd_channel, "catalog": cmd_catalog, "verify": cmd_verify, "plan": cmd_plan,
        "replan": cmd_replan, "history": cmd_history, "events": cmd_events, "recorder": cmd_recorder, "player": cmd_player, "weather-break": cmd_weather_break, "era-id": cmd_era_id, "prefetch": cmd_prefetch,
        "activate-show": lambda c, x: cmd_show(c, x, True),
        "deactivate-show": lambda c, x: cmd_show(c, x, False),
    }
    try:
        rc = handlers[a.cmd](cp, a)
    except (ReplanRefused, WeatherBreakRefused, PolicyError, ValueError, LookupError) as exc:
        print(f"wdbs: {exc}", file=sys.stderr)
        return 2
    return rc or 0
