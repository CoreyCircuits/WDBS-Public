"""Tell the running FS42 player to reload its schedule, and prove it did.

Uses the existing FS42 API server on 127.0.0.1:<server_port> (no new listener) and
the reload-schedule patch (wdbs.fs42_patch). The player writes
runtime/schedule_reload.ack; we match our request id and compare the schedule
extents it loaded with the DB.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import fs42_adapter as fs
from .config import Settings


def _port(settings: Settings) -> int:
    return int(settings.fs42_main_config.get("server_port", 4242))


def ack_path(settings: Settings):
    return settings.fs42_root / "runtime" / "schedule_reload.ack"


def request_reload(settings: Settings, timeout_s: float = 30.0, station: str | None = None) -> dict:
    rid = uuid.uuid4().hex[:16]
    url = (f"http://127.0.0.1:{_port(settings)}/player/commands/reload_schedule?"
           + urllib.parse.urlencode({"request_id": rid}))
    out: dict = {"request_id": rid, "requested_at": dt.datetime.now().isoformat(sep=" ", timespec="seconds")}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="POST", data=b""), timeout=10) as r:
            out["api"] = json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        out.update(ok=False, available=False if e.code in (404, 405) else None,
                   error=f"HTTP {e.code}" + (" - running player predates the reload patch; "
                                             "restart it to load the patch" if e.code in (404, 405) else ""))
        return out
    except (urllib.error.URLError, OSError) as e:
        out.update(ok=False, available=None, error=f"FS42 API unreachable: {e}")
        return out

    deadline = time.time() + timeout_s
    ack = None
    while time.time() < deadline:
        try:
            data = json.loads(ack_path(settings).read_text())
            if data.get("request_id") == rid:
                ack = data
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    if ack is None:
        out.update(ok=False, available=True, error=f"no acknowledgement within {timeout_s:.0f}s "
                   "(player busy, stalled, or on a non-schedule channel loop)")
        return out
    out["ack"] = ack
    checks = {"player reported ok": bool(ack.get("ok"))}
    with fs.connect_ro(settings.fs42_db) as conn:
        for name, (s, e) in (ack.get("extents") or {}).items():
            if station and name != station:
                continue
            db_s, db_e = fs.schedule_extent(conn, name)
            checks[f"{name} extents match DB"] = (s == (db_s.isoformat() if db_s else None)
                                                  and e == (db_e.isoformat() if db_e else None))
            n = conn.execute("SELECT count(*) FROM liquid_blocks WHERE station = ?", (name,)).fetchone()[0]
            checks[f"{name} block count matches DB"] = (ack.get("blocks") or {}).get(name) == n
    out["checks"] = checks
    out["ok"] = all(checks.values())
    out["available"] = True
    return out
