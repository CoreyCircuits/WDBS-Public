"""WDBS weather-prewarm: start the next web entry's window (the WeatherStar weather break) a few seconds
early, "shown" to Qt/Chromium but never mapped on screen (WA_DontShowOnScreen), so the page has loaded
its data by the time the entry starts. The entry then only maps the window instead of cold-starting Qt
(~1.5 s of black) and ws4kp (~3.5 s of loading screen) on air.

Measured on the TV host (2026-09-28): a cold start shows Current Conditions ~5 s into the segment; a window
pre-warmed 6 s ahead shows it 0.2 s after it is mapped, and stays unmapped on X until then.

Every path fails safe to FS42's stock cold start: no pre-warmed window, a dead one, or one for another
entry means show_web spawns as before. An un-adopted window closes itself after TIMEOUT_S.
WDBS_PREWARM=0 disables; WDBS_PREWARM_LEAD sets the lead in seconds (default 8).
"""

import logging
import multiprocessing
import os
import time
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

LEAD_S = float(os.environ.get("WDBS_PREWARM_LEAD", "8"))
TIMEOUT_S = 30  # the window quits itself if nobody shows it by then (skipped break, channel change)

_l = logging.getLogger("WDBSPrewarm")


def enabled():
    return os.environ.get("WDBS_PREWARM", "1") != "0"


def _is_web(entry):
    from fs42.autobump_agent import AutoBumpAgent
    return AutoBumpAgent.is_autobump_url(getattr(entry, "path", "") or "")


def arm(player, play_point, entry):
    """At the start of `entry`: find the next web entry in this break and how long after `entry` ends it
    starts. Only looks inside the current break (stops at a feature) and within LEAD_S."""
    player._wdbs_prewarm_target = None
    if not enabled():
        return
    plan = play_point.plan
    pos = next((i for i, e in enumerate(plan) if e is entry), None)
    if pos is None:
        return
    gap = 0.0
    for nxt in plan[pos + 1:]:
        if _is_web(nxt):
            player._wdbs_prewarm_target = (nxt, gap)
            return
        if getattr(nxt, "content_type", None) == "feature":
            return
        gap += nxt.duration or 0
        if gap > LEAD_S:
            return


def tick(player, time_remaining):
    """Called from the playback wait loop: spawn once the web entry is LEAD_S away."""
    target = getattr(player, "_wdbs_prewarm_target", None)
    if not target or time_remaining + target[1] > LEAD_S:
        return
    player._wdbs_prewarm_target = None
    try:
        spawn(player, target[0])
    except Exception:
        _l.exception("pre-warm failed; the entry will cold-start")
        drop(player)


def web_url(entry):
    """The URL FS42's play_file builds for a web entry played from its start."""
    from fs42.autobump_agent import AutoBumpAgent
    url = AutoBumpAgent.extract_url(entry.path)
    if entry.duration:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        params["duration"] = [str(int(entry.duration * 1000))]
        url = urlunparse(parsed._replace(query=urlencode({k: v[0] for k, v in params.items()})))
    return url


def spawn(player, entry):
    warm = getattr(player, "_wdbs_warm", None)
    if warm and warm["entry"] is entry and warm["proc"].is_alive():
        return
    drop(player)
    from fs42.webrender.web_render import web_render_runner
    conf = {"web_url": web_url(entry), "wdbs_prewarm": True, "wdbs_prewarm_timeout": TIMEOUT_S}
    if entry.duration:
        conf["duration"] = entry.duration
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(target=web_render_runner, args=(conf, queue))
    proc.start()
    player._wdbs_warm = {"entry": entry, "path": entry.path, "queue": queue, "proc": proc, "t": time.time()}
    _l.info(f"WDBS pre-warm: started web window {LEAD_S:.0f}s ahead (pid {proc.pid})")


def take(player, path):
    """show_web: hand over the pre-warmed (queue, process) for `path`, or None to cold-start."""
    warm = getattr(player, "_wdbs_warm", None)
    player._wdbs_warm = None
    if not warm:
        return None
    if path and warm["path"] == path and warm["proc"].is_alive():
        _l.info(f"WDBS pre-warm: adopting window warmed {time.time() - warm['t']:.1f}s ago")
        return warm["queue"], warm["proc"]
    _close(warm)
    return None


def drop_stale(player, play_point):
    """A new play point (block start, channel change): keep the window only if its entry is in this plan."""
    warm = getattr(player, "_wdbs_warm", None)
    if warm and not any(e is warm["entry"] for e in play_point.plan):
        drop(player)


def drop(player):
    warm = getattr(player, "_wdbs_warm", None)
    player._wdbs_warm = None
    player._wdbs_prewarm_target = None
    if warm:
        _close(warm)


def _close(warm):
    try:
        warm["queue"].put("hide_window")
        warm["proc"].join(timeout=1.5)
        if warm["proc"].is_alive():
            warm["proc"].terminate()
            warm["proc"].join(timeout=1)
        _l.info("WDBS pre-warm: closed an unused window")
    except Exception:
        _l.exception("WDBS pre-warm: close failed")
