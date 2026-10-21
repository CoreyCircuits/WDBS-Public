#!/usr/bin/env python3
"""Keep owner-chosen posters in Sonarr (app host).

Sonarr always shows TheTVDB's primary poster and has no per-series choice; for revived shows that can be the
wrong era (All That's primary poster is the 2019 revival). Sonarr also re-downloads the primary poster on its
periodic series refresh whenever its cached copy differs. This puts the pinned poster back afterwards.

For each pin in config/poster_pins.json: find the series in Sonarr by TVDB id, render the pinned poster once
into the cache (poster.jpg + the 500/250 px sizes Sonarr's UI uses), and replace any of Sonarr's cached
MediaCover files that differ (temp file + rename, so Sonarr never serves a half-written image).

    poster_pins.py [--dry-run]

Touches only Sonarr's MediaCover/<series id>/poster*.jpg; reads Sonarr's API with the bridge's key.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / "config" / "poster_pins.json"
ENV_FILE = Path.home() / ".config" / "wdbs-notion-sonarr.env"
SONARR_CONFIG = Path(os.environ.get("WDBS_SONARR_CONFIG", str(Path.home() / "media-automation/config/sonarr")))
CACHE = Path.home() / ".cache" / "wdbs" / "poster-pins"
SIZES = {"poster.jpg": None, "poster-500.jpg": 500, "poster-250.jpg": 250}
ALLOWED_HOST = "https://artworks.thetvdb.com/"


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    return env


def load_pins(path: Path) -> list[dict]:
    pins = json.loads(path.read_text()).get("pins", [])
    for p in pins:
        if not isinstance(p.get("tvdb"), int):
            raise ValueError(f"pin without an integer tvdb id: {p}")
        if not str(p.get("poster", "")).startswith(ALLOWED_HOST):
            raise ValueError(f"{p.get('title')}: poster must be an {ALLOWED_HOST} URL")
    return pins


def render(source: bytes) -> dict[str, bytes]:
    """The three files Sonarr serves, from one source image."""
    from PIL import Image
    img = Image.open(io.BytesIO(source)).convert("RGB")
    out = {}
    for name, width in SIZES.items():
        im = img if width is None else img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92 if width is None else 90)
        out[name] = buf.getvalue()
    return out


def stale(folder: Path, rendered: dict[str, bytes]) -> list[str]:
    """Which of Sonarr's cached files differ from the pinned rendering (missing counts as different)."""
    return [name for name, data in rendered.items()
            if not (folder / name).is_file() or (folder / name).read_bytes() != data]


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.wdbs-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def fetch(url: str, headers: dict | None = None) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=30) as r:
        return r.read()


def cached_rendering(pin: dict) -> dict[str, bytes]:
    d = CACHE / str(pin["tvdb"])
    src_name = pin["poster"].rsplit("/", 1)[-1]
    src = d / f"source-{src_name}"
    if not src.is_file():
        d.mkdir(parents=True, exist_ok=True)
        write_atomic(src, fetch(pin["poster"]))
    rendered = {}
    for name in SIZES:
        f = d / f"{src_name}.{name}"
        if not f.is_file():
            rendered = render(src.read_bytes())
            for n, data in rendered.items():
                write_atomic(d / f"{src_name}.{n}", data)
            break
    return {name: (d / f"{src_name}.{name}").read_bytes() for name in SIZES}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    env = load_env(ENV_FILE)
    base = env.get("SONARR_URL", "http://127.0.0.1:8989").rstrip("/")
    series = json.loads(fetch(f"{base}/api/v3/series", {"X-Api-Key": env["SONARR_API_KEY"]}))
    by_tvdb = {s.get("tvdbId"): s for s in series}

    failed = 0
    for pin in load_pins(PINS):
        s = by_tvdb.get(pin["tvdb"])
        if not s:
            print(f"{pin['title']}: not in Sonarr, skipped")
            continue
        try:
            rendered = cached_rendering(pin)
        except Exception as exc:  # one bad pin must not stop the others
            print(f"{pin['title']}: could not get the pinned poster: {exc}")
            failed += 1
            continue
        folder = SONARR_CONFIG / "MediaCover" / str(s["id"])
        todo = stale(folder, rendered)
        if not todo:
            print(f"{pin['title']}: pinned poster in place")
            continue
        if a.dry_run:
            print(f"{pin['title']}: would restore {', '.join(todo)}")
            continue
        folder.mkdir(parents=True, exist_ok=True)
        for name in todo:
            write_atomic(folder / name, rendered[name])
        print(f"{pin['title']}: restored pinned poster ({', '.join(todo)})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
