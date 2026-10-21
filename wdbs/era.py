"""Show eras for the era-matched WDBS logos on Channel 10.

Spec: docs/superpowers/specs/2026-09-28-era-logos-design.md, section 1.
A show's era is its Notion `Era Override` when set, otherwise the decade of its `Premiere Year`:
before 1980 -> 70s, 1980s -> 80s, 1990s -> 90s, 2000 or later (or unknown) -> 00s, the everyday
finish. The app host rotation exporter copies both Notion fields into the rotation manifest as
`premiere_year` and `era_override`; this module is the only place the rule lives.
"""

from __future__ import annotations

ERAS = ("70s", "80s", "90s", "00s")
DEFAULT_ERA = "00s"
_OVERRIDES = {"70s": "70s", "80s": "80s", "90s": "90s", "2000s": "00s", "00s": "00s"}


def era_for(premiere_year, override=None) -> str:
    if isinstance(override, str) and override.strip() in _OVERRIDES:
        return _OVERRIDES[override.strip()]
    try:
        year = int(premiere_year)
    except (TypeError, ValueError):
        return DEFAULT_ERA
    if year < 1980:
        return "70s"
    if year < 1990:
        return "80s"
    if year < 2000:
        return "90s"
    return DEFAULT_ERA


def tag_eras(programs: list[dict]) -> dict[str, str]:
    """tag -> era for every manifest program that has a tag."""
    return {p["tag"]: era_for(p.get("premiere_year"), p.get("era_override"))
            for p in programs if p.get("tag")}
