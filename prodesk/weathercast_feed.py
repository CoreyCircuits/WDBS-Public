#!/usr/bin/env python3
"""WeatherCast feed (app host): live ticker, severe-weather alerts, sky/season for the WDBS weather channel.

Every run (systemd timer, every 2 min):
  1. reads the next Channel 10 programs from WDBS on the TV host (`wdbs --json next`);
  2. reads active NWS alerts for the configured point and the latest observation;
  3. writes `weathercast.json` into the ws4kp data folder (served at /data/weathercast.json); the
     WeatherStar page (custom.js) swaps in the ticker text, turns the crawl red in alert mode and
     picks the emblem variant for the sky and season;
  4. on a new NWS *Warning*: pushes FS42's red on-screen ticker over whatever is playing, repeats it
     every ALERT_REPEAT_MIN while the warning is active, and asks WDBS for one extra weather break
     in the soonest Channel 10 block (`wdbs weather-break --asap --auto`, the same guarded write path);
  5. keeps the ws4kp music set seasonal (holiday tracks join in December, if any are in the library).

Stdlib only. Settings come from ~/.config/wdbs-weathercast.env (KEY=value lines); defaults below.
Usage: weathercast_feed.py [--dry-run] [--simulate-alert "Tornado Warning"] [--no-actions]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
DEFAULTS = {
    "LAT": "40.0000",
    "LON": "-75.0000",
    "CITY": "Exampleville",
    "OBS_STATION": "KLOU",
    "CHANNEL": "10",
    "BRAND": "WDBS 4 WEATHERCAST",
    "TV_HOST": "TV_HOST",
    "WDBS": "~/.local/bin/wdbs",
    "INTRO": "/path/to/FieldStation42/wdbs_assets/weather_intro.mp4",
    "MORNING_INTRO": "/path/to/FieldStation42/wdbs_assets/weather_intro_morning.mp4",
    "USE_INTROS": "0",          # 1 once the weather-group catch-up rule is live
    "ALERT_EXTRA_BREAK": "1",
    "ALERT_REPEAT_MIN": "10",
    "WS4KP_DIR": str(HOME / "ws4kp-data"),
    "STATE": str(HOME / ".local/state/wdbs/weathercast-state.json"),
}
UA = "(wdbs-weathercast, local)"
SMALL_WORDS = {"and", "of", "the", "a", "an", "in", "on", "at", "to", "for"}
HOLIDAY_SPAN = ((12, 1), (12, 26))   # holiday tracks join the music set between these dates (inclusive)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    env = HOME / ".config/wdbs-weathercast.env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def log(msg: str):
    print(f"{dt.datetime.now():%H:%M:%S} {msg}", flush=True)


def get_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def fetch_warnings(cfg: dict) -> list[dict]:
    data = get_json(f"https://api.weather.gov/alerts/active?point={cfg['LAT']},{cfg['LON']}")
    out = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        if p.get("status") != "Actual" or not str(p.get("event", "")).endswith("Warning"):
            continue
        out.append({"id": p.get("id") or f.get("id"), "event": p["event"], "headline": p.get("headline") or "",
                    "ends": p.get("ends") or p.get("expires"), "area": p.get("areaDesc") or ""})
    return out


def fetch_observation(cfg: dict) -> dict:
    p = get_json(f"https://api.weather.gov/stations/{cfg['OBS_STATION']}/observations/latest")["properties"]
    c = (p.get("temperature") or {}).get("value")
    return {"text": p.get("textDescription") or "", "temp_f": round(c * 9 / 5 + 32) if c is not None else None}


def ssh(cfg: dict, command: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", cfg["TV_HOST"], command],
                          capture_output=True, text=True, timeout=timeout)


def fetch_next_programs(cfg: dict) -> list[dict]:
    r = ssh(cfg, f"{cfg['WDBS']} --json next -c {int(cfg['CHANNEL'])} -n 3", timeout=45)
    if r.returncode != 0:
        raise RuntimeError(f"wdbs next failed: {(r.stderr or r.stdout).strip()[-200:]}")
    return json.loads(r.stdout).get("next", [])


# --------------------------------------------------------------------------
# Derived values
# --------------------------------------------------------------------------

def show_name(tag: str | None) -> str:
    words = (tag or "").replace("_", " ").split()
    return " ".join(w if (i and w in SMALL_WORDS) else w.capitalize() for i, w in enumerate(words)) or "WDBS"


def episode_title(file_path: str | None) -> str | None:
    """'Seinfeld - S01E05 - The Stock Tip.mkv' -> 'The Stock Tip' (Sonarr naming only)."""
    m = re.match(r".+? - S\d+E\d+ - (.+)\.\w+$", os.path.basename(file_path or ""))
    return m.group(1) if m else None


def clock(ts: str) -> str:
    t = dt.datetime.fromisoformat(ts)
    return t.strftime("%I:%M").lstrip("0")


def up_next_text(cfg: dict, programs: list[dict]) -> str:
    if not programs:
        return ""
    first, rest = programs[0], programs[1:]
    ep = episode_title(first.get("file_path"))
    parts = [f"UP NEXT ON WDBS {cfg['CHANNEL']}: {show_name(first.get('program_tag'))}"
             + (f' "{ep}"' if ep else "") + f" at {clock(first['start'])}"]
    parts += [f"then {show_name(p.get('program_tag'))} at {clock(p['start'])}" for p in rest]
    return " - ".join(parts)


def sky_of(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("snow", "sleet", "ice", "freezing")):
        return "snow"
    if "thunder" in t:
        return "storm"
    if any(w in t for w in ("rain", "drizzle", "shower")):
        return "rain"
    if any(w in t for w in ("clear", "sunny", "fair")):
        return "clear"
    return "cloudy"


def season_of(d: dt.date) -> str:
    md = (d.month, d.day)
    if (3, 20) <= md < (6, 21):
        return "spring"
    if (6, 21) <= md < (9, 22):
        return "summer"
    if (9, 22) <= md <= (11, 30):
        return "fall"
    return "winter"


def alert_text(w: dict) -> str:
    ends = ""
    if w.get("ends"):
        try:
            ends = " until " + dt.datetime.fromisoformat(w["ends"]).astimezone().strftime("%I:%M %p").lstrip("0")
        except ValueError:
            pass
    return f"{w['event'].upper()}{ends}. {w['headline']} Stay with WeatherCast on Channel 4."


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------

def write_feed(cfg: dict, feed: dict):
    target = Path(cfg["WS4KP_DIR"]) / "data" / "weathercast.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(feed, indent=1))
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)


def push_fs42_ticker(cfg: dict, w: dict) -> bool:
    body = json.dumps({"message": alert_text(w), "header": "WEATHER ALERT", "style": "breaking", "iterations": 3})
    cmd = ("curl -s -m 10 -X POST -H 'Content-Type: application/json' "
           f"--data {shlex.quote(body)} http://127.0.0.1:4242/player/ticker")
    r = ssh(cfg, cmd, timeout=30)
    ok = r.returncode == 0 and "success" in r.stdout
    log(f"FS42 alert ticker {'pushed' if ok else 'FAILED'}: {w['event']} {r.stdout.strip()[:80]} {r.stderr.strip()[:120]}")
    return ok


def request_extra_break(cfg: dict) -> bool:
    cmd = f"{cfg['WDBS']} weather-break --channel {int(cfg['CHANNEL'])} --asap --auto"
    if cfg.get("USE_INTROS") == "1":
        cmd += f" --intro {cfg['INTRO']} --morning-intro {cfg['MORNING_INTRO']}"
    r = ssh(cfg, cmd, timeout=600)
    tail = " | ".join(line for line in r.stdout.splitlines() if "airs at" in line or "Result:" in line)
    log(f"extra weather break: {tail or (r.stderr or r.stdout).strip()[-200:]}")
    return r.returncode == 0


def sync_music(cfg: dict, today: dt.date) -> str:
    """Active ws4kp music = library/regular (+ library/holiday in the holiday span). Hard links, same filesystem."""
    base = Path(cfg["WS4KP_DIR"])
    active, lib = base / "music", base / "music-library"
    if not (lib / "regular").is_dir():
        return "no music library; skipped"
    wanted = {p.name: p for p in (lib / "regular").glob("*.mp3")}
    holiday = HOLIDAY_SPAN[0] <= (today.month, today.day) <= HOLIDAY_SPAN[1]
    if holiday and (lib / "holiday").is_dir():
        wanted.update({p.name: p for p in (lib / "holiday").glob("*.mp3")})
    managed = {p.name for d in ("regular", "holiday") if (lib / d).is_dir() for p in (lib / d).glob("*.mp3")}
    added = removed = 0
    for name, src in wanted.items():
        dst = active / name
        if not dst.exists():
            os.link(src, dst)
            added += 1
    for p in active.glob("*.mp3"):
        if p.name not in wanted and p.name in managed:   # never touch files the library doesn't know
            p.unlink()
            removed += 1
    return f"{len(wanted)} tracks{' (holiday)' if holiday else ''}; +{added} -{removed}"


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print what would happen; write nothing, push nothing")
    ap.add_argument("--no-actions", action="store_true", help="write the feed, but no FS42 ticker / extra break")
    ap.add_argument("--simulate-alert", metavar="EVENT", help='inject a fake warning, e.g. "Tornado Warning"')
    a = ap.parse_args()
    cfg = load_config()
    now = dt.datetime.now()
    state_path = Path(cfg["STATE"]).expanduser()
    state = json.loads(state_path.read_text()) if state_path.exists() else {"alerts": {}}

    feed = {"generated": now.isoformat(timespec="seconds"), "brand": cfg["BRAND"], "season": season_of(now.date()),
            "sky": "rain", "temp_f": None, "mode": "normal", "ticker": cfg["BRAND"], "alerts": [], "errors": []}

    try:
        obs = fetch_observation(cfg)
        feed.update(sky=sky_of(obs["text"]), temp_f=obs["temp_f"], conditions=obs["text"])
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        feed["errors"].append(f"observation: {e}")

    try:
        warnings = fetch_warnings(cfg)
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        warnings = []
        feed["errors"].append(f"alerts: {e}")
    if a.simulate_alert:
        warnings.append({"id": f"simulated-{now:%Y%m%d%H%M}", "event": a.simulate_alert,
                         "headline": "THIS IS A TEST of the WeatherCast alert system.", "ends": None, "area": cfg["CITY"]})

    try:
        up_next = up_next_text(cfg, fetch_next_programs(cfg))
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as e:
        up_next = ""
        feed["errors"].append(f"schedule: {e}")

    if warnings:
        feed.update(mode="alert", alerts=warnings,
                    ticker="   ***   ".join(alert_text(w) for w in warnings))
    else:
        feed["ticker"] = f"{cfg['BRAND']} - {up_next}" if up_next else cfg["BRAND"]

    if a.dry_run:
        print(json.dumps(feed, indent=1))
        return 0
    write_feed(cfg, feed)
    log(f"feed: mode={feed['mode']} sky={feed['sky']} season={feed['season']} errors={len(feed['errors'])}")
    try:
        log(f"music: {sync_music(cfg, now.date())}")
    except OSError as e:
        log(f"music: sync failed: {e}")

    if not a.no_actions:
        repeat = dt.timedelta(minutes=int(cfg["ALERT_REPEAT_MIN"]))
        for w in warnings:
            seen = state["alerts"].get(w["id"])
            if seen is None:
                seen = state["alerts"][w["id"]] = {"first": now.isoformat(timespec="seconds"), "ticker": None, "extra_break": False}
                log(f"NEW WARNING: {w['event']} — {w['headline'][:120]}")
            if seen["ticker"] is None or now - dt.datetime.fromisoformat(seen["ticker"]) >= repeat:
                if push_fs42_ticker(cfg, w):
                    seen["ticker"] = now.isoformat(timespec="seconds")
            if cfg["ALERT_EXTRA_BREAK"] == "1" and not seen["extra_break"]:
                seen["extra_break"] = True          # once per warning, even if it fails (no retry storm)
                request_extra_break(cfg)
        active = {w["id"] for w in warnings}
        state["alerts"] = {k: v for k, v in state["alerts"].items()
                           if k in active or now - dt.datetime.fromisoformat(v["first"]) < dt.timedelta(days=2)}
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
