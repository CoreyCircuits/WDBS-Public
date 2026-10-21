"""WDBS catch-up policy for FieldStation42 playout (BUG-002).

Installed into fs42/ by `wdbs.fs42_patch` (patch "catchup"). Wall clock stays
the anchor; this only decides *where lateness goes*:

  * protected feature starts - a feature segment joined up to PROTECT_FEATURE_S
    late (block overran, start timeout + retry) restarts from its planned start
    instead of losing its opening;
  * no near-end break joins - a commercial/bump joined with < MIN_BREAK_REMAINING_S
    left or past half way is skipped rather than shown for a few seconds;
  * lateness is absorbed by breaks - while playout is at least a whole
    commercial/bump behind the block timeline, that entry is skipped. Features
    are never shortened or skipped;
  * tail hold - when a block's content ends a few seconds before the block does
    (plan rounding), hold until the boundary instead of failing into standby.
  * weather groups (owner decision 2026-09-27) - consecutive entries with
    content_type "weather" (intro + WeatherStar segment) are ONE unit: skipped
    only when playout is late by at least the group's total length, never
    partially; joining mid-group plays the rest. "bump"/"commercial" unchanged.

Set WDBS_CATCHUP=0 in the player's environment to disable everything here.
"""

import datetime
import os
import time

ENABLED = os.environ.get("WDBS_CATCHUP", "1") != "0"
PROTECT_FEATURE_S = 60.0
MIN_BREAK_REMAINING_S = 10.0
MAX_TAIL_HOLD_S = 120.0
BREAK_TYPES = ("commercial", "bump")
GROUP_TYPES = ("weather",)
_current_plan = None
_group_decisions = {}


def _ctype(entry):
    return getattr(entry, "content_type", None) or "feature"


def adjust_play_point(play_point, log=None):
    """Adjust a wall-clock play point in place (see module doc). Returns it."""
    if not ENABLED or play_point is None or not getattr(play_point, "plan", None):
        return play_point
    idx, off = play_point.index, float(play_point.offset or 0)
    if idx >= len(play_point.plan):
        return play_point
    entry = play_point.plan[idx]
    kind = _ctype(entry)
    dur = float(getattr(entry, "duration", 0) or 0)
    if kind in BREAK_TYPES and off > 0 and (dur - off < MIN_BREAK_REMAINING_S or off > dur / 2):
        if idx + 1 < len(play_point.plan):
            if log:
                log.info(f"WDBS catch-up: skipping {kind} joined at {off:.1f}/{dur:.1f}s")
            play_point.index = idx + 1
            play_point.offset = 0
            return adjust_play_point(play_point, log)
    elif kind == "feature" and 0 < off <= PROTECT_FEATURE_S:
        if log:
            log.info(f"WDBS catch-up: protecting feature start (would have skipped {off:.1f}s)")
        play_point.offset = 0
    return play_point


def annotate(liquid, network_name, when, play_point):
    """Attach the block start so playout can measure lateness against the block timeline."""
    global _current_plan
    _current_plan = getattr(play_point, "plan", None)
    try:
        block = liquid.get_programming_block(network_name, when)
        play_point.wdbs_block_start = block.start_time if block is not None else None
    except Exception:
        play_point.wdbs_block_start = None
    return play_point


def planned_start(play_point):
    """Planned wall-clock start of plan[index], or None if unknown."""
    start = getattr(play_point, "wdbs_block_start", None)
    if start is None:
        return None
    before = sum(float(getattr(e, "duration", 0) or 0) for e in play_point.plan[:play_point.index])
    return start + datetime.timedelta(seconds=before)


def _group_decision(plan, entry, lateness_s):
    idx = next((i for i, e in enumerate(plan) if e is entry), None)
    if idx is None:
        return False
    start = idx
    while start > 0 and _ctype(plan[start - 1]) in GROUP_TYPES:
        start -= 1
    key = (id(plan), start)
    if key not in _group_decisions:
        if len(_group_decisions) > 512:
            _group_decisions.clear()
        if idx != start:
            _group_decisions[key] = False  # joined mid-group: play the rest
        else:
            end = idx
            while end + 1 < len(plan) and _ctype(plan[end + 1]) in GROUP_TYPES:
                end += 1
            total = sum(float(getattr(e, "duration", 0) or 0) for e in plan[start:end + 1])
            _group_decisions[key] = lateness_s >= total
    return _group_decisions[key]


def should_skip_break(entry, lateness_s, plan=None):
    """Skip a whole commercial/bump while we are at least its length behind.
    Weather entries are decided as one group (all or nothing)."""
    if not ENABLED:
        return False
    kind = _ctype(entry)
    if kind in GROUP_TYPES:
        plan = plan if plan is not None else _current_plan
        return bool(plan) and _group_decision(plan, entry, lateness_s)
    return kind in BREAK_TYPES and lateness_s >= float(getattr(entry, "duration", 0) or 0)


def tail_hold_until(liquid, network_name, when):
    """If `when` is inside a block whose content already ended, return the block end."""
    if not ENABLED:
        return None
    try:
        block = liquid.get_programming_block(network_name, when)
    except Exception:
        return None
    if block is None or when >= block.end_time:
        return None
    if (block.end_time - when).total_seconds() > MAX_TAIL_HOLD_S:
        return None
    return block.end_time


def hold(player, until):
    """Wait until `until` while servicing input (channel changes etc.)."""
    from fs42.station_player import PlayerOutcome, PlayerState
    player._l.info(f"WDBS catch-up: block content ended early; holding until {until.strftime('%H:%M:%S')}")
    while datetime.datetime.now() < until:
        time.sleep(0.05)
        response = player.input_check_fn()
        if response:
            if player.handle_runtime_command_outcome(response):
                continue
            if player.is_non_interrupting(response):
                continue
            return response
    return PlayerOutcome(PlayerState.SUCCESS)
