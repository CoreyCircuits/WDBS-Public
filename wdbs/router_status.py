"""`wdbs router` - read-only view of the Channel Router (docs/CHANNEL-ROUTER.md).

Shows who owns the screen right now (~/.local/state/wdbs/router.json, written by the FS42 remote-app patch on
every tune), checks the remote-app registry, and says whether the client (Moonlight) is installed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("WDBS_STATE_DIR", str(Path.home() / ".local/state/wdbs"))) / "router.json"
APPS = Path(os.environ.get("WDBS_REMOTE_APPS", str(REPO / "config/remote_apps.json")))
MOONLIGHT = Path(os.environ.get("WDBS_MOONLIGHT", str(Path.home() / ".local/bin/moonlight")))


def _core():
    spec = importlib.util.spec_from_file_location("wdbs_router_core", REPO / "fs42_overlay/wdbs_router_core.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("wdbs_router_core", mod)   # dataclasses look their module up while executing
    spec.loader.exec_module(mod)
    return mod


def report() -> dict:
    core = _core()
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = None
    apps, error = {}, None
    try:
        for key, app in core.load_apps(json.loads(APPS.read_text())).items():
            ready = app.provider != "sunshine" or bool(app.host and app.application)
            apps[key] = {"name": app.name, "provider": app.provider, "host": app.host, "application": app.application,
                         "configured": ready}
    except (OSError, ValueError) as e:
        error = str(e)
    return {"state": state, "registry": str(APPS), "registry_error": error, "apps": apps,
            "moonlight": str(MOONLIGHT) if MOONLIGHT.exists() else None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wdbs router", description="Channel Router status (read-only)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = report()
    if a.json:
        print(json.dumps(r, indent=2))
        return 0
    st = r["state"]
    if st:
        age = f"{time.time() - st['at']:.0f} s ago" if st.get("at") else "?"
        print(f"Mode      {st['mode']}  (since tune {age})")
        print(f"Channel   {st['channel']} {st['name']}  backend={st['backend']}  previous={st['previous_channel']}")
        if st.get("app"):
            print(f"App       {st['app']}  session={st['session']}" + (f"  reason: {st['reason']}" if st.get("reason") else ""))
    else:
        print("Mode      unknown (no router.json yet: remote-app patch not active or no tune since)")
    print(f"Registry  {r['registry']}" + (f"  ERROR: {r['registry_error']}" if r["registry_error"] else ""))
    for key, app in r["apps"].items():
        where = f"{app['host'] or '(no host)'} / {app['application'] or '-'}" if app["provider"] == "sunshine" else "local command"
        print(f"  {key:<12} {app['provider']:<9} {where}  {'ready' if app['configured'] else 'NOT SET UP'}")
    print(f"Moonlight {r['moonlight'] or 'NOT INSTALLED'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
