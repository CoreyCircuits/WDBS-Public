#!/usr/bin/env python3
"""WDBS Today feed: builds home.json for the WDBS Today web channel.

Runs on the app host from wdbs-today-feed.timer (every 5 minutes). Reads:
  - Google Calendar through each calendar's secret iCal address (WDBS_TODAY_ICS_URLS)
  - Notion Tasks and Projects databases (NOTION_TOKEN)
  - National Weather Service for Exampleville (no key)
  - the optional Ledger service GET /v1/summary on the app host (LEDGER_URL, LEDGER_TOKEN; optional)
and writes one JSON file that the channel page polls.

Money comes only from the optional Ledger service's derived summary. This feed never talks to
Actual Budget, and the Ledger token stays in this process: home.json carries only the
allowlisted summary fields, never the token or any transaction data.

Every source is independent. When one fails, the last good copy of that source is
kept, the failure is recorded under "health", and the page shows it in the ticker.
The output is written atomically, so the page never reads a half-written file.

Secrets come only from the environment (EnvironmentFile in the systemd unit) and are
never logged.

  wdbs_today_feed.py --config today-config.json --out ~/wdbs-today/www/home.json
  wdbs_today_feed.py --dry-run            # print the JSON, write nothing
  wdbs_today_feed.py --fixtures DIR       # offline run against saved responses
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = 1
HTTP_TIMEOUT = 20


# ----------------------------------------------------------------------------
# sources (live network, or a fixtures directory for offline testing)
# ----------------------------------------------------------------------------
class LiveSources:
    def __init__(self, user_agent: str):
        self.ua = user_agent

    def _get(self, url: str, headers: dict | None = None, data: bytes | None = None) -> bytes:
        h = {"User-Agent": self.ua}
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.read()

    def ics(self, url: str) -> bytes:
        return self._get(url)

    def notion_query(self, db_id: str, body: dict, token: str, version: str) -> dict:
        raw = self._get(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": version,
                     "Content-Type": "application/json"},
            data=json.dumps(body).encode(),
        )
        return json.loads(raw)

    def nws(self, url: str) -> dict:
        return json.loads(self._get(url, headers={"Accept": "application/geo+json"}))

    def ledger(self, base_url: str, token: str) -> dict:
        req = urllib.request.Request(base_url.rstrip("/") + "/v1/summary", method="GET",
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        # Ledger is on this host: never route it through a proxy from the environment.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as r:
            return json.loads(r.read(64 * 1024))


class FixtureSources:
    """Reads saved responses: calendar-*.ics, notion-tasks.json, notion-projects.json,
    nws-points.json, nws-forecast.json, nws-stations.json, nws-observation.json, nws-alerts.json,
    ledger-summary.json"""

    def __init__(self, root: str):
        self.root = Path(root)

    def ics(self, url: str) -> bytes:
        return (self.root / url.split("/")[-1]).read_bytes()

    def notion_query(self, db_id: str, body: dict, token: str, version: str) -> dict:
        name = "notion-projects.json" if "Lane" in json.dumps(body) else "notion-tasks.json"
        return json.loads((self.root / name).read_text())

    def nws(self, url: str) -> dict:
        if "/points/" in url:
            name = "nws-points.json"
        elif "/forecast" in url:
            name = "nws-forecast.json"
        elif url.endswith("/stations"):
            name = "nws-stations.json"
        elif "/observations/latest" in url:
            name = "nws-observation.json"
        else:
            name = "nws-alerts.json"
        return json.loads((self.root / name).read_text())

    def ledger(self, base_url: str, token: str) -> dict:
        return json.loads((self.root / "ledger-summary.json").read_text())


# ----------------------------------------------------------------------------
# calendar
# ----------------------------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_note(text, limit: int) -> str:
    if not text:
        return ""
    t = html.unescape(TAG_RE.sub(" ", str(text)))
    t = WS_RE.sub(" ", t).strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def parse_ics(raw: bytes, start: dt.datetime, end: dt.datetime, tz: ZoneInfo, cfg: dict) -> list[dict]:
    import icalendar  # pip: icalendar
    import recurring_ical_events  # pip: recurring-ical-events

    cal = icalendar.Calendar.from_ical(raw)
    cal_name = str(cal.get("X-WR-CALNAME", "") or "")
    skip = {s.lower() for s in cfg.get("skip_titles", [])}
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        title = str(ev.get("SUMMARY", "") or "Untitled").strip()
        if title.lower() in skip:
            continue
        s = ev.decoded("DTSTART")
        e = ev.decoded("DTEND") if ev.get("DTEND") else None
        all_day = isinstance(s, dt.date) and not isinstance(s, dt.datetime)
        if all_day:
            e = e or (s + dt.timedelta(days=1))
            start_s, end_s = s.isoformat(), e.isoformat()
        else:
            if s.tzinfo is None:
                s = s.replace(tzinfo=tz)
            if e is None:
                dur = ev.decoded("DURATION") if ev.get("DURATION") else dt.timedelta(hours=1)
                e = s + dur
            elif e.tzinfo is None:
                e = e.replace(tzinfo=tz)
            start_s, end_s = s.astimezone(tz).isoformat(), e.astimezone(tz).isoformat()
        note = clean_note(ev.get("DESCRIPTION"), cfg.get("note_max_chars", 120)) if cfg.get("show_notes", True) else ""
        out.append({"title": title, "start": start_s, "end": end_s, "all_day": all_day,
                    "note": note, "calendar": cal_name})
    return out


# ----------------------------------------------------------------------------
# notion
# ----------------------------------------------------------------------------
def prop_value(props: dict, name: str):
    p = props.get(name)
    if not p:
        return None
    t = p.get("type")
    v = p.get(t)
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in (v or [])).strip() or None
    if t in ("select", "status"):
        return v.get("name") if v else None
    if t == "date":
        return v.get("start") if v else None
    if t in ("number", "checkbox", "url", "email"):
        return v
    if t == "formula" and v:
        return v.get(v.get("type"))
    return None


def notion_all(src, db_id: str, body: dict, token: str, version: str) -> list[dict]:
    rows, cursor = [], None
    for _ in range(10):
        b = dict(body, page_size=100)
        if cursor:
            b["start_cursor"] = cursor
        res = src.notion_query(db_id, b, token, version)
        rows.extend(res.get("results", []))
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return rows


def fetch_tasks(src, cfg: dict, token: str, today: dt.date) -> list[dict]:
    n, p = cfg["notion"], cfg["notion"]["tasks"]
    horizon = (today + dt.timedelta(days=1)).isoformat()
    body = {"filter": {"and": [
        {"property": p["done"], "checkbox": {"equals": False}},
        {"or": [
            {"property": p["scheduled"], "date": {"on_or_before": horizon}},
            {"property": p["due"], "date": {"on_or_before": horizon}},
        ]},
    ]}}
    out = []
    for row in notion_all(src, n["tasks_db"], body, token, n["api_version"]):
        pr = row.get("properties", {})
        if prop_value(pr, p["done"]):
            continue
        title = prop_value(pr, p["title"])
        if not title:
            continue
        sched, due = prop_value(pr, p["scheduled"]), prop_value(pr, p["due"])
        minutes = prop_value(pr, p["minutes"])
        out.append({
            "title": title,
            "priority": prop_value(pr, p["priority"]),
            "area": prop_value(pr, p["area"]),
            "minutes": int(minutes) if isinstance(minutes, (int, float)) else None,
            "scheduled": sched[:10] if sched else None,
            "due": due[:10] if due else None,
        })
    return out[:30]


def fetch_burners(src, cfg: dict, token: str) -> list[dict]:
    n, p = cfg["notion"], cfg["notion"]["projects"]
    body = {"filter": {"property": p["lane"], "select": {"equals": p["lane_value"]}}}
    out = []
    for row in notion_all(src, n["projects_db"], body, token, n["api_version"]):
        pr = row.get("properties", {})
        title = prop_value(pr, p["title"])
        if not title:
            continue
        dl = prop_value(pr, p["deadline"])
        out.append({"title": title, "vault": prop_value(pr, p["vault"]),
                    "next": prop_value(pr, p["next"]) or "", "deadline": dl[:10] if dl else None})
    out.sort(key=lambda b: (b["deadline"] is None, b["deadline"] or "", b["title"].lower()))
    return out[:8]


# ----------------------------------------------------------------------------
# weather (National Weather Service)
# ----------------------------------------------------------------------------
def fetch_weather(src, cfg: dict, state: dict) -> dict:
    w = cfg["weather"]
    nws_state = state.setdefault("nws", {})
    base = "https://api.weather.gov"
    key = f"{w['lat']:.4f},{w['lon']:.4f}"
    if nws_state.get("point") != key or not nws_state.get("forecast_url"):
        pts = src.nws(f"{base}/points/{key}")["properties"]
        nws_state.update(point=key, forecast_url=pts["forecast"], stations_url=pts["observationStations"],
                         station=None)
    if not nws_state.get("station"):
        feats = src.nws(nws_state["stations_url"]).get("features", [])
        nws_state["station"] = feats[0]["properties"]["stationIdentifier"] if feats else None

    periods = src.nws(nws_state["forecast_url"])["properties"]["periods"][:3]
    temp, sky = None, None
    if nws_state.get("station"):
        try:
            obs = src.nws(f"{base}/stations/{nws_state['station']}/observations/latest")["properties"]
            c = (obs.get("temperature") or {}).get("value")
            temp = round(c * 9 / 5 + 32) if isinstance(c, (int, float)) else None
            sky = obs.get("textDescription") or None
        except Exception:  # observation is optional; the forecast still stands
            pass
    alerts = []
    try:
        for f in src.nws(f"{base}/alerts/active?point={key}").get("features", [])[:3]:
            a = f.get("properties", {})
            alerts.append({"event": a.get("event"), "headline": a.get("headline"), "severity": a.get("severity"),
                           "ends": a.get("ends") or a.get("expires")})
    except Exception:
        pass

    high = low = None
    if periods:
        p0 = periods[0]
        if p0.get("isDaytime"):
            high = p0.get("temperature")
            if len(periods) > 1 and not periods[1].get("isDaytime"):
                low = periods[1].get("temperature")
        else:
            low = p0.get("temperature")
    if temp is None and periods:
        temp = periods[0].get("temperature")
    return {
        "temp": temp,
        "sky": sky or (periods[0].get("shortForecast") if periods else None),
        "high": high,
        "low": low,
        "periods": [{"name": p.get("name"),
                     "text": f"{p.get('shortForecast')}, {'high' if p.get('isDaytime') else 'low'} {p.get('temperature')}"}
                    for p in periods[:2]],
        "alerts": alerts,
    }


# ----------------------------------------------------------------------------
# money (the optional Ledger service)
# ----------------------------------------------------------------------------
MONEY_AMOUNTS = ("available", "spent_today", "upcoming_7d")
MONEY_STATUS = ("clear", "watch", "short")
# Bank freshness (Ledger computes it from Actual's own last successful bank sync, never from read time). A Ledger that sends none
# is treated as "unknown" (fail closed): the page must not present bank data as current without evidence.
MONEY_BANK_STATES = ("manual", "fresh", "stale", "unknown", "error")


def parse_money(raw) -> dict:
    """Copies only the fields Channel 3 shows, after checking each one. Anything else Ledger
    sends (spent_month, currency, a future field) is dropped, never passed to the page."""
    if not isinstance(raw, dict):
        raise ValueError("ledger: summary is not an object")
    out = {}
    for k in MONEY_AMOUNTS:
        v = raw.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) > 1e9:
            raise ValueError(f"ledger: bad {k}")
        out[k] = round(float(v), 2)
    if out["spent_today"] < 0 or out["upcoming_7d"] < 0:
        raise ValueError("ledger: negative outflow")
    if raw.get("status") not in MONEY_STATUS:
        raise ValueError("ledger: bad status")
    out["status"] = raw["status"]
    out["as_of"] = dt.datetime.fromisoformat(str(raw.get("as_of", "")).replace("Z", "+00:00")).isoformat(timespec="seconds")
    state = raw.get("bank_state", "unknown")
    if state not in MONEY_BANK_STATES:
        raise ValueError("ledger: bad bank_state")
    out["bank_state"] = state
    synced = raw.get("bank_synced_at")
    out["bank_synced_at"] = None if synced is None else dt.datetime.fromisoformat(str(synced).replace("Z", "+00:00")).isoformat(timespec="seconds")
    age = raw.get("bank_data_age_hours")
    if age is not None and (isinstance(age, bool) or not isinstance(age, (int, float)) or age != age or age < 0 or age > 1e6):
        raise ValueError("ledger: bad bank_data_age_hours")
    out["bank_data_age_hours"] = None if age is None else round(float(age), 1)
    return out


def ledger_error(e: Exception) -> str:
    """Error text for health/logs: the class and HTTP status only, so nothing upstream can leak into home.json."""
    code = getattr(e, "code", None)
    return f"{type(e).__name__}" + (f" HTTP {code}" if isinstance(code, int) else "")


# ----------------------------------------------------------------------------
# mode
# ----------------------------------------------------------------------------
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def pick_mode(events: list[dict], cfg: dict, today: dt.date) -> dict:
    modes = cfg["modes"]
    today_s = today.isoformat()
    todays = [e for e in events
              if (e["all_day"] and e["start"][:10] <= today_s < e["end"][:10])
              or (not e["all_day"] and e["start"][:10] == today_s)]
    for e in todays:
        if not e["all_day"]:
            continue
        t = e["title"].lower().strip()
        for key, m in modes.items():
            if any(t == k or t.startswith(k) for k in m.get("all_day_titles", [])):
                return {"key": key, "reason": f"your “{e['title']}” event"}
    for e in todays:
        if e["all_day"]:
            continue
        t = e["title"].lower()
        for key, m in modes.items():
            for kw in m.get("timed_keywords", []):
                if re.search(r"\b" + re.escape(kw.lower()) + r"\b", t):
                    return {"key": key, "reason": f"your “{e['title']}” event"}
    wk = cfg.get("weekday_modes", {}).get(WEEKDAYS[today.weekday()])
    if wk in modes:
        return {"key": wk, "reason": f"the {today.strftime('%A')} default"}
    return {"key": cfg.get("default_mode", "build"), "reason": "default"}


# ----------------------------------------------------------------------------
# music bed
# ----------------------------------------------------------------------------
AUDIO_EXT = {".ogg", ".opus", ".oga", ".webm", ".flac", ".wav", ".mp3", ".m4a"}
LEADING_NUM = re.compile(r"^\d+[\s._-]+")


def scan_music(folder: str, www: Path) -> list[dict]:
    """List playable files in the music folder as URLs relative to the page."""
    root = Path(folder).expanduser()
    if not root.is_dir():
        return []
    out = []
    for f in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if f.name.startswith(".") or f.suffix.lower() not in AUDIO_EXT or not f.is_file():
            continue
        try:
            rel = f.resolve().relative_to(www.resolve())
        except ValueError:
            continue  # the page can only reach files inside www/
        title = LEADING_NUM.sub("", f.stem).replace("_", " ").strip()
        out.append({"src": urllib.parse.quote(rel.as_posix()), "title": title})
    return out


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_atomic(path: Path, obj, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def health_entry(prev: dict, ok: bool, now_iso: str, error: str | None = None) -> dict:
    if ok:
        return {"ok": True, "at": now_iso}
    since = prev.get("since") if prev and prev.get("ok") is False else now_iso
    return {"ok": False, "since": since, "error": (error or "")[:200]}


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Build home.json for the WDBS Today channel.")
    ap.add_argument("--config", default=str(here / "today-config.json"))
    ap.add_argument("--out", default="~/wdbs-today/www/home.json")
    ap.add_argument("--state", default="~/wdbs-today/state.json")
    ap.add_argument("--dry-run", action="store_true", help="print the JSON instead of writing it")
    ap.add_argument("--fixtures", help="read saved responses from this folder instead of the network")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).expanduser().read_text())
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    out_path = Path(args.out).expanduser()
    state_path = Path(args.state).expanduser()
    prev = load_json(out_path, {})
    state = load_json(state_path, {})
    prev_health = prev.get("health", {})

    now = dt.datetime.now(tz)
    now_iso = now.isoformat(timespec="seconds")
    today = now.date()
    src = FixtureSources(args.fixtures) if args.fixtures else LiveSources(cfg["weather"]["user_agent"])
    health, errors = {}, 0

    # calendar: each iCal address is cached on its own, so one broken calendar
    # doesn't freeze the others
    ics_urls = [u for u in re.split(r"[\s,]+", os.environ.get("WDBS_TODAY_ICS_URLS", "")) if u]
    if args.fixtures and not ics_urls:
        ics_urls = [f"fixture://{p.name}" for p in sorted(Path(args.fixtures).glob("calendar-*.ics"))]
    win_start = dt.datetime.combine(today, dt.time.min, tz)
    win_end = win_start + dt.timedelta(days=int(cfg["calendar"].get("days", 2)))
    ics_cache = state.setdefault("ics", {})
    events, cal_err = [], None
    for url in ics_urls:
        key = hashlib.sha256(url.encode()).hexdigest()[:16]  # never store the secret URL itself
        try:
            evs = parse_ics(src.ics(url), win_start, win_end, tz, cfg["calendar"])
            ics_cache[key] = {"events": evs, "at": now_iso}
        except Exception as e:  # keep that calendar's last good events
            cal_err = f"{type(e).__name__}: {e}"
            evs = ics_cache.get(key, {}).get("events", [])
        events.extend(evs)
    if not ics_urls:
        cal_err = "WDBS_TODAY_ICS_URLS is not set"
    events.sort(key=lambda e: (e["start"], e["title"]))
    health["calendar"] = health_entry(prev_health.get("calendar", {}), cal_err is None, now_iso, cal_err)
    errors += cal_err is not None

    # notion
    token = os.environ.get("NOTION_TOKEN", "")
    tasks, burners = prev.get("tasks", []), prev.get("burners", [])
    try:
        if not token and not args.fixtures:
            raise RuntimeError("NOTION_TOKEN is not set")
        tasks = fetch_tasks(src, cfg, token, today)
        burners = fetch_burners(src, cfg, token)
        health["notion"] = health_entry({}, True, now_iso)
    except Exception as e:
        health["notion"] = health_entry(prev_health.get("notion", {}), False, now_iso, f"{type(e).__name__}: {e}")
        errors += 1

    # weather, cached for a few minutes to go easy on the NWS
    weather = prev.get("weather")
    wcache = state.get("weather_at")
    fresh = False
    if wcache and weather:
        try:
            fresh = (now - dt.datetime.fromisoformat(wcache)).total_seconds() < 60 * cfg["weather"].get("cache_minutes", 10)
        except ValueError:
            fresh = False
    if fresh and prev_health.get("weather", {}).get("ok", True):
        health["weather"] = prev_health.get("weather", {"ok": True, "at": wcache})
    else:
        try:
            weather = fetch_weather(src, cfg, state)
            state["weather_at"] = now_iso
            health["weather"] = health_entry({}, True, now_iso)
        except Exception as e:
            health["weather"] = health_entry(prev_health.get("weather", {}), False, now_iso, f"{type(e).__name__}: {e}")
            errors += 1

    # money: the optional Ledger service's derived summary. Off unless LEDGER_URL is set (or running fixtures).
    ledger_url = os.environ.get("LEDGER_URL", "")
    money = prev.get("money") if (ledger_url or args.fixtures) else None  # off means no money on screen
    if ledger_url or args.fixtures:
        try:
            ledger_token = os.environ.get("LEDGER_TOKEN", "")
            if not ledger_token and not args.fixtures:
                raise RuntimeError("LEDGER_TOKEN is not set")
            money = parse_money(src.ledger(ledger_url, ledger_token))
            health["money"] = health_entry({}, True, now_iso)
        except Exception as e:  # keep the last good summary; the page says it's stale
            health["money"] = health_entry(prev_health.get("money", {}), False, now_iso, ledger_error(e))
            errors += 1

    # ticker extras from other WDBS jobs (one line each, # for comments)
    ticker = list(cfg.get("ticker", []))
    extra = cfg.get("ticker_extra_file")
    if extra:
        p = Path(extra).expanduser()
        if p.exists():
            ticker += [ln.strip() for ln in p.read_text().splitlines() if ln.strip() and not ln.startswith("#")][:10]

    mcfg = cfg.get("music", {})
    tracks = scan_music(mcfg.get("folder", "~/wdbs-today/www/music"), out_path.parent)

    feed = {
        "version": VERSION,
        "generated": now_iso,
        "name": cfg.get("name", ""),
        "look": cfg.get("look", {}),
        "location": {"lat": cfg["weather"]["lat"], "lon": cfg["weather"]["lon"]},
        "mode": pick_mode(events, cfg, today),
        "modes": {k: {"name": v["name"], "directive": v["directive"]} for k, v in cfg["modes"].items()},
        "weather": weather,
        "events": events,
        "tasks": tasks,
        "burners": burners,
        "money": money,
        "routines": cfg.get("routines", {}),
        "lists": cfg.get("lists", {}),
        "ticker": ticker,
        "music": {"tracks": tracks, "volume": mcfg.get("volume", 0.7), "night_volume": mcfg.get("night_volume", 0.35)},
        "health": health,
    }

    if args.dry_run:
        print(json.dumps(feed, ensure_ascii=False, indent=1))
    else:
        write_atomic(out_path, feed)
        write_atomic(state_path, state, 0o600)
    print(f"wdbs-today-feed: events={len(events)} tasks={len(tasks)} burners={len(burners)} "
          f"weather={'ok' if health.get('weather', {}).get('ok') else 'stale'} "
          f"mode={feed['mode']['key']} tracks={len(tracks)} "
          f"money={'off' if 'money' not in health else 'ok' if health['money']['ok'] else 'stale'} errors={errors}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
