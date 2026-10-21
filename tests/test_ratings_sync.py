"""prodesk/wdbs_ratings_sync.py: JSONL ratings -> Notion rows."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "wdbs_ratings_sync", Path(__file__).resolve().parents[1] / "prodesk" / "wdbs_ratings_sync.py")
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

AD = {"id": "a1", "at": "2026-09-28T13:36:40-04:00", "daypart": "Daytime", "thumb": "down", "reason": "Glitch/stall",
      "channel": 10, "network": "EXAMPLE SITCOMS", "title": "The Nanny", "kind": "commercial", "show_tag": None,
      "episode": None, "episode_title": None, "commercial_file": "WDBS__2005-09__2007---Zyrtec-Drug-Commercial__45s__6179.mp4",
      "file": "/x/WDBS__2005-09__2007---Zyrtec-Drug-Commercial__45s__6179.mp4", "position": "0:12:07",
      "late_s": 3.2, "slate": None, "log": ["WARNING slow"], "label": "Zyrtec Drug Commercial"}
EP = {**AD, "id": "e1", "thumb": "up", "reason": None, "kind": "episode", "show_tag": "nanny", "episode": "S01E09",
      "episode_title": "Personal Business", "commercial_file": None, "label": "The Nanny S01E09"}


class Fake:
    """Stands in for Notion: ids that exist, a show index, a spot finder; can fail on the Nth create."""
    def __init__(self, existing=(), fail_on=None):
        self.existing, self.created, self.fail_on = set(existing), [], fail_on

    def __call__(self, method, path, body=None):
        if path.endswith("/query") and "Rating ID" in json.dumps(body):
            rid = body["filter"]["rich_text"]["equals"]
            return {"results": [{"id": "x"}] if rid in self.existing else [], "has_more": False}
        if path.endswith("/query") and "FS42 Tag" in json.dumps(body or {}):
            return {"results": [{"id": "show-nanny", "properties": {"FS42 Tag": {"rich_text": [{"plain_text": "nanny"}]}}}],
                    "has_more": False}
        if path.endswith("/query"):
            return {"results": [{"id": "spot-zyrtec"}], "has_more": False}
        if path == "/pages":
            if self.fail_on is not None and len(self.created) == self.fail_on:
                raise rs.NotionError("503")
            self.created.append(body)
            return {"id": f"p{len(self.created)}"}
        raise AssertionError(path)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.state = Path(self.td.name) / "sync.json"

    def tearDown(self):
        self.td.cleanup()

    def lines(self, *recs):
        return lambda start: [json.dumps(r) for r in recs][start:]

    def test_properties_and_title(self):
        p = rs.properties(AD, None, "spot-zyrtec")
        self.assertEqual(p["Rating"]["title"][0]["text"]["content"], "👎 Zyrtec Drug Commercial · Glitch/stall")
        self.assertEqual(p["Thumb"]["select"]["name"], "👎")
        self.assertEqual(p["Kind"]["select"]["name"], "Commercial")
        self.assertEqual(p["Commercial"]["relation"], [{"id": "spot-zyrtec"}])
        self.assertEqual(p["Review"]["select"]["name"], "New")
        self.assertEqual(p["When"]["date"]["start"], "2026-09-28T13:36:40-04:00")
        self.assertEqual(rs.properties(EP, "show-nanny", None)["Episode"]["rich_text"][0]["text"]["content"],
                         "S01E09 Personal Business")

    def test_snapshot_goes_in_the_body_in_chunks(self):
        big = {**AD, "log": ["ERROR " + "x" * 290] * 20}
        blocks = rs.children(big, [])
        code = blocks[0]["code"]["rich_text"]
        self.assertTrue(all(len(t["text"]["content"]) <= 1900 for t in code))
        self.assertEqual(json.loads("".join(t["text"]["content"] for t in code))["id"], "a1")

    def test_sync_creates_rows_and_advances(self):
        fake = Fake()
        n = rs.sync(self.lines(AD, EP), fake, self.state)
        self.assertEqual(n, 2)
        self.assertEqual(json.loads(self.state.read_text())["line"], 2)
        self.assertEqual(fake.created[1]["properties"]["Show"]["relation"], [{"id": "show-nanny"}])
        self.assertEqual(rs.sync(self.lines(AD, EP), fake, self.state), 0)         # nothing new

    def test_existing_rating_is_skipped(self):
        fake = Fake(existing={"a1"})
        self.assertEqual(rs.sync(self.lines(AD, EP), fake, self.state), 1)
        self.assertEqual(len(fake.created), 1)

    def test_failure_keeps_state_at_last_success(self):
        fake = Fake(fail_on=1)
        with self.assertRaises(rs.NotionError):
            rs.sync(self.lines(AD, EP), fake, self.state)
        self.assertEqual(json.loads(self.state.read_text())["line"], 1)
        fake.fail_on = None
        self.assertEqual(rs.sync(self.lines(AD, EP), fake, self.state), 1)          # retried, not duplicated

    def test_unmatched_show_still_lands_with_a_note(self):
        stray = {**EP, "id": "s1", "show_tag": "not_a_show"}
        fake = Fake()
        rs.sync(self.lines(stray), fake, self.state)
        body = fake.created[0]
        self.assertEqual(body["properties"]["Show"]["relation"], [])
        self.assertIn("No WDBS Programming row", json.dumps(body["children"]))

    def test_bad_line_is_skipped_and_counted(self):
        fake = Fake()
        rs.sync(lambda start: ["{not json", json.dumps(AD)][start:], fake, self.state)
        self.assertEqual((len(fake.created), json.loads(self.state.read_text())["line"]), (1, 2))


class FetchLinesTests(unittest.TestCase):
    """A U+2028 inside a JSON string (append_jsonl writes ensure_ascii=False) must not shift line
    counting vs `tail -n +N`: splitlines() would break that one physical line into two."""

    def _run(self, stdout: str) -> list[str]:
        completed = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr="")
        with mock.patch.object(rs.subprocess, "run", return_value=completed):
            return rs.fetch_lines(0)

    def test_line_separator_inside_a_string_does_not_split_the_line(self):
        rec = json.dumps({"id": "a1", "note": "line1 line2"})
        other = json.dumps({"id": "a2"})
        lines = self._run(rec + "\n" + other + "\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["note"], "line1 line2")
        self.assertEqual(json.loads(lines[1])["id"], "a2")

    def test_trailing_newline_does_not_add_an_empty_line(self):
        self.assertEqual(self._run(json.dumps({"id": "a1"}) + "\n"), [json.dumps({"id": "a1"})])

    def test_empty_output_is_no_lines(self):
        self.assertEqual(self._run(""), [])
