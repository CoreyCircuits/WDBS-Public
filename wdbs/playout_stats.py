"""Playout quality for a time window, from the TV host journal (fs42-player + wdbs-slate).

    wdbs playout-stats [--since "2026-09-30 17:00"] [--until now] [--json]

Plays = FS42 "playing <path>" lines; a start timeout is FS42's "Timeout waiting for playback to start" (the
player rests 1 s and retries, so the viewer sees a standby slate); "no response" is FS42's "No response from
MPV" (mpv stopped answering its IPC, e.g. blocked on a stalled read). Slate durations come from wdbs-slate's
show/hide pairs: start = slow start, stall = freeze mid-program. Read-only; run it on the TV host.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys

_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)")
_SLATE = re.compile(r"wdbs\.slate: (?:show (\w+)|(hide))")


def _journal(unit: str, since: str, until: str) -> list[str]:
    r = subprocess.run(["journalctl", "--user", "-u", unit, "--since", since, "--until", until,
                        "--no-pager", "-o", "short-iso"], capture_output=True, text=True)
    return r.stdout.splitlines()


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(len(v) * q))]


def player_counts(lines: list[str]) -> dict:
    plays = ads = timeouts = ad_timeouts = no_response = 0
    for line in lines:
        if "INFO:FieldPlayer:playing /" in line:
            plays += 1
            ads += "/commercial/" in line
        elif "Timeout waiting for playback to start" in line:
            timeouts += 1
            ad_timeouts += "/commercial/" in line
        elif "No response from MPV" in line:
            no_response += 1
    return {"plays": plays, "ad_plays": ads, "start_timeouts": timeouts, "ad_timeouts": ad_timeouts,
            "episode_timeouts": timeouts - ad_timeouts, "no_response_from_mpv": no_response,
            "timeout_rate": round(timeouts / plays, 4) if plays else None}


def slate_durations(lines: list[str]) -> dict:
    cur, out = None, {}
    for line in lines:
        ts, m = _TS.match(line), _SLATE.search(line)
        if not ts or not m:
            continue
        t = dt.datetime.fromisoformat(ts.group(1))
        if m.group(1):
            cur = (t, m.group(1))
        elif cur:
            out.setdefault(cur[1], []).append((t - cur[0]).total_seconds())
            cur = None
    return {kind: {"n": len(v), "median_s": _pct(v, 0.5), "p90_s": _pct(v, 0.9), "max_s": max(v),
                   "total_min": round(sum(v) / 60, 1)} for kind, v in out.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wdbs playout-stats", description=__doc__.splitlines()[0])
    ap.add_argument("--since", default="-24h")
    ap.add_argument("--until", default="now")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = {"since": a.since, "until": a.until,
         "player": player_counts(_journal("fs42-player", a.since, a.until)),
         "slates": slate_durations(_journal("wdbs-slate", a.since, a.until))}
    if a.json:
        print(json.dumps(r, indent=2))
        return 0
    p = r["player"]
    rate = f"{p['timeout_rate']:.1%}" if p["timeout_rate"] is not None else "-"
    print(f"Playout {a.since} -> {a.until}")
    print(f"  plays {p['plays']} ({p['ad_plays']} ads)   start timeouts {p['start_timeouts']} = {rate} "
          f"(ads {p['ad_timeouts']}, episodes {p['episode_timeouts']})   no response from mpv {p['no_response_from_mpv']}")
    for kind, s in sorted(r["slates"].items()):
        print(f"  slate {kind:8} n={s['n']:4}  median {s['median_s']:5.1f}s  p90 {s['p90_s']:5.1f}s  "
              f"max {s['max_s']:6.1f}s  total {s['total_min']:5.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
