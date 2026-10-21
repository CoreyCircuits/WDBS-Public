"""Child-process entry point that runs FS42's own block builder.

Reads a JSON job on stdin, pins every FS42 IO class to job["db_path"], and
calls LiquidSchedule._fluid(start, end) for one station. Runs in its own
process so the db_path override and FS42's singletons cannot leak into the
caller. Must be launched with cwd = FS42 root and the FS42 venv interpreter.

This does NOT call reset_schedule, rebuild_sequences, scan_sequences,
_write_catalog, or any catalog rebuild path.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import os
import sys


def _date_key_matches(date_key: str, when: dt.date) -> bool:
    """Same rule as FS42 SlotReader._date_key_matches ("April 23" or "December 24 - January 2")."""
    try:
        parts = [x.strip() for x in date_key.split(" - ")]
        md = (when.month, when.day)
        if len(parts) == 1:
            d = dt.datetime.strptime(parts[0], "%B %d")
            return (d.month, d.day) == md
        if len(parts) == 2:
            a, b = (dt.datetime.strptime(x, "%B %d") for x in parts)
            a, b = (a.month, a.day), (b.month, b.day)
            return a <= md <= b if a <= b else (md >= a or md <= b)
    except ValueError:
        pass
    return False


def merge_date_overrides(ours: dict, config: dict, start: dt.datetime, end: dt.datetime):
    """Merge the engine's per-day overrides with the station config's, hour by hour.

    FS42 takes the FIRST date key that matches a day and only then looks up the hour, so an
    engine key for a day would hide the config's override for every hour of that day. Here each
    engine day inherits the config's slots for hours the engine does not set; where both set an
    hour the engine wins (that is the replan) and a warning is returned."""
    merged, warnings = {}, []
    day = start.date()
    while day <= end.date():
        key = day.strftime("%B %d").replace(" 0", " ")
        if key in ours:
            slots = dict(ours[key])
            match = next((k for k in config if _date_key_matches(k, day)), None)
            if match is not None:
                for hour, slot in config[match].items():
                    if str(hour) in slots:
                        warnings.append(f"{day} {hour}:00 - lineup replaces the config date override {match!r}")
                    else:
                        slots[str(hour)] = slot
            merged[key] = slots
        day += dt.timedelta(days=1)
    for k, v in ours.items():  # keys outside the range are kept as given
        merged.setdefault(k, v)
    for k, v in config.items():
        merged.setdefault(k, v)
    return merged, warnings


def main() -> int:
    job = json.loads(sys.stdin.read())
    root = job["fs42_root"]
    os.chdir(root)
    sys.path.insert(0, root)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    from fs42.station_manager import StationManager  # noqa: E402

    sm = StationManager()
    sm.server_conf["db_path"] = job["db_path"]

    from fs42.liquid_schedule import LiquidSchedule  # noqa: E402

    base = sm.station_by_name(job["station"])
    if base is None:
        print(json.dumps({"error": f"station {job['station']!r} not found"}))
        return 3
    conf = copy.deepcopy(base)
    start = dt.datetime.fromisoformat(job["start"])
    end = dt.datetime.fromisoformat(job["end"])
    overrides = job.get("date_overrides")
    warnings = []
    if overrides:
        conf["date_overrides"], warnings = merge_date_overrides(overrides, conf.get("date_overrides") or {},
                                                                start, end)
        for w in warnings:
            print(f"WARNING: {w}", file=sys.stderr)

    schedule = LiquidSchedule(conf)
    schedule._fluid(start, end)
    print(json.dumps({"built_from": job["start"], "built_to": job["end"],
                      "used_date_overrides": sorted((overrides or {}).keys()), "warnings": warnings}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
