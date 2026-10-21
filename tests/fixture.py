"""Synthetic FieldStation42-style environment built from a minimal, independently authored schema
(tests/fixtures/wdbs_fs42_min_schema.sql; see its header for provenance and limits)."""

from __future__ import annotations

import datetime as dt
import json
import socket
import sqlite3
import tempfile
from pathlib import Path

from wdbs.config import Settings

SCHEMA = Path(__file__).parent / "fixtures" / "wdbs_fs42_min_schema.sql"
STATION = "TEST SITCOMS"
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

SHOWS = {
    "alpha": [f"Alpha - S01E{n:02d} - Ep{n}.mkv" for n in range(1, 13)],
    "bravo": [f"Bravo - S01E{n:02d} - Ep{n}.mkv" for n in range(1, 9)],
    "charlie": [f"Charlie - S01E{n:02d} - Ep{n}.mkv" for n in range(1, 5)],
}
TEMPLATE = {str(h): ["alpha", "bravo", "charlie"][h % 3] for h in range(24)}


class FakeFS42:
    """Temp FS42 root: confs/, runtime/fs42_fluid.db, catalog/<tag>/ files."""

    def __init__(self, day0: dt.datetime, days: int = 2, template: dict | None = None):
        self.tmp = tempfile.TemporaryDirectory(prefix="wdbs-test-")
        self.root = Path(self.tmp.name) / "FieldStation42"
        self.template = template or TEMPLATE
        (self.root / "confs").mkdir(parents=True)
        (self.root / "runtime").mkdir()
        self.content_dir = self.root / "catalog" / "test"
        self.db = self.root / "runtime" / "fs42_fluid.db"
        self.state_db = Path(self.tmp.name) / "state" / "wdbs.db"
        self.manifest = Path(self.tmp.name) / "manifest.json"
        self.api_port = _free_port()
        # Private port: tests must never reach the live FS42 API on 4242.
        (self.root / "confs" / "main_config.json").write_text(json.dumps({"server_port": self.api_port}))
        self._write_conf()
        self._write_db()
        self.build(day0, day0 + dt.timedelta(days=days))
        self.write_manifest()

    def cleanup(self):
        self.tmp.cleanup()

    @property
    def settings(self) -> Settings:
        return Settings(fs42_root=self.root, state_db=self.state_db, manifest=self.manifest)

    def _write_conf(self):
        slots = {h: {"tags": t, "sequence": t} for h, t in self.template.items()}
        conf = {"station_conf": {
            "network_name": STATION, "channel_number": 10, "network_type": "standard",
            "content_dir": str(self.content_dir), "schedule_increment": 30,
            "day_templates": {"auto": slots}, **{d: "auto" for d in DAYS},
            "commercial_dir": "commercial", "bump_dir": "bump", "break_strategy": "standard"}}
        (self.root / "confs" / "test.json").write_text(json.dumps(conf))

    def conn(self):
        return sqlite3.connect(str(self.db))

    def _write_db(self):
        c = self.conn()
        c.executescript(SCHEMA.read_text())
        for tag, files in SHOWS.items():
            d = self.content_dir / tag / "Season 1"
            d.mkdir(parents=True)
            paths = []
            for f in files:
                p = d / f
                p.write_bytes(b"x")
                paths.append(str(p))
                c.execute("INSERT INTO catalog_entries (station, path, realpath, title, duration, tag, count, "
                          "content_type) VALUES (?, ?, ?, ?, ?, ?, 0, 'feature')",
                          (STATION, str(p), str(p), f[:-4], 1320.0, tag))
            cur = c.execute("INSERT INTO named_sequence (station, sequence_name, tag_path, start_perc, end_perc, "
                            "current_index, initialized) VALUES (?, ?, ?, 0.0, 1.0, 0, 1)", (STATION, tag, tag))
            for i, p in enumerate(sorted(paths)):
                c.execute("INSERT INTO sequence_entries (fpath, sequence_index, named_sequence_id) VALUES (?, ?, ?)",
                          (p, i, cur.lastrowid))
        c.commit()
        c.close()

    def write_manifest(self, programs: list[dict] | None = None, generated_at: str | None = None):
        programs = programs or [
            {"tag": "alpha", "program": "Alpha", "dayparts": ["Prime", "Late Night"], "weight": 2.0},
            {"tag": "bravo", "program": "Bravo", "dayparts": ["Morning", "Daytime"], "weight": 1.0},
            {"tag": "charlie", "program": "Charlie", "dayparts": ["Early", "Morning", "Daytime", "After School"],
             "weight": 1.0},
        ]
        self.manifest.write_text(json.dumps({
            "schema": 1, "channel": 10,
            "generated_at": generated_at or dt.datetime.now(dt.timezone.utc).isoformat(),
            "programs": programs}))

    # A faithful-enough mimic of LiquidSchedule._fluid for sequence-only stations.
    def build(self, start: dt.datetime, end: dt.datetime, date_overrides: dict | None = None,
              db: Path | None = None):
        build_blocks(db or self.db, self.template, start, end, date_overrides)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_blocks(db: Path, template: dict, start: dt.datetime, end: dt.datetime,
                 date_overrides: dict | None = None):
    c = sqlite3.connect(str(db))
    cur_mark = start
    while cur_mark < end:
        key = cur_mark.strftime("%B %d").replace(" 0", " ")
        tag = ((date_overrides or {}).get(key, {}).get(str(cur_mark.hour), {}) or {}).get("tags") \
            or template[str(cur_mark.hour)]
        if isinstance(tag, list):  # FS42 split hour: [a, b] -> a at :00-:29, b at :30-:59
            tag = tag[min(cur_mark.minute // (60 // len(tag)), len(tag) - 1)]
        sid, idx = c.execute("SELECT id, current_index FROM named_sequence WHERE station = ? AND sequence_name = ?",
                             (STATION, tag)).fetchone()
        eps = [r[0] for r in c.execute("SELECT fpath FROM sequence_entries WHERE named_sequence_id = ? "
                                       "ORDER BY fpath", (sid,))]
        if idx >= len(eps):
            idx = 0
        path = eps[idx]
        c.execute("UPDATE named_sequence SET current_index = ? WHERE id = ?", (idx + 1, sid))
        cid, title = c.execute("SELECT id, title FROM catalog_entries WHERE station = ? AND path = ?",
                               (STATION, path)).fetchone()
        c.execute("UPDATE catalog_entries SET count = count + 1 WHERE id = ?", (cid,))
        nxt = cur_mark + dt.timedelta(minutes=30)
        plan = [{"path": path, "skip": 0, "duration": 1320.0, "is_stream": False, "content_type": "feature"}]
        c.execute("INSERT INTO liquid_blocks (station, liquid_type, start_time, end_time, break_strategy, title, "
                  "sequence_key, break_info, content_json, plan_json) VALUES (?, 'LiquidBlock', ?, ?, 'standard', "
                  "?, ?, NULL, ?, ?)",
                  (STATION, cur_mark.strftime("%Y-%m-%d %H:%M:%S"), nxt.strftime("%Y-%m-%d %H:%M:%S"), title,
                   json.dumps({"station_name": STATION, "sequence_name": tag, "tag_path": tag}),
                   json.dumps(cid), json.dumps(plan)))
        cur_mark = nxt
    c.commit()
    c.close()
