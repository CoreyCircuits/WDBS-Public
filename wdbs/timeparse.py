"""Operator-friendly time expressions: 'now', 'tomorrow 06:00', '+2h', ISO."""

from __future__ import annotations

import datetime as dt
import re

_REL = re.compile(r"^\+(\d+)\s*([mhd])$")
_HM = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_when(text: str, now: dt.datetime) -> dt.datetime:
    s = text.strip().lower()
    if s == "now":
        return now.replace(microsecond=0)
    m = _REL.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"m": dt.timedelta(minutes=n), "h": dt.timedelta(hours=n), "d": dt.timedelta(days=n)}[unit]
        return (now + delta).replace(microsecond=0)
    parts = s.split()
    if parts and parts[0] in ("today", "tomorrow"):
        day = now.date() + dt.timedelta(days=1 if parts[0] == "tomorrow" else 0)
        hm = parts[1] if len(parts) > 1 else "00:00"
        m = _HM.match(hm)
        if not m:
            raise ValueError(f"Expected HH:MM after {parts[0]!r}: {text!r}")
        return dt.datetime.combine(day, dt.time(int(m.group(1)), int(m.group(2))))
    m = _HM.match(s)
    if m:
        cand = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return cand if cand > now else cand + dt.timedelta(days=1)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized time: {text!r} (try 'tomorrow 06:00', '+2h', or '2026-09-27 06:00')")
