"""Episode identification from media file names."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

_PATTERNS = [
    re.compile(r"(?i)\bS(\d{1,2})[ ._-]?E(\d{1,3})\b"),
    re.compile(r"(?i)(?<![\d])(\d{1,2})x(\d{2,3})\b"),
]


@dataclass(frozen=True)
class EpisodeId:
    season: int | None
    episode: int | None

    @property
    def code(self) -> str | None:
        if self.season is None or self.episode is None:
            return None
        return f"S{self.season:02d}E{self.episode:02d}"


def parse_episode(path_or_title: str | None) -> EpisodeId:
    if not path_or_title:
        return EpisodeId(None, None)
    name = os.path.basename(path_or_title)
    for pat in _PATTERNS:
        m = pat.search(name)
        if m:
            return EpisodeId(int(m.group(1)), int(m.group(2)))
    return EpisodeId(None, None)
