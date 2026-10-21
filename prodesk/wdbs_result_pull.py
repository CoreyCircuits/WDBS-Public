#!/usr/bin/env python3
"""app host: pull the TV host's overnight catalog result so the Notion exporter can feed it back.

The TV host writes FieldStation42/runtime/wdbs_channel10_catalog_result.json locally
(its media share is read-only). The exporter's consume_results() reads
WDBS_CATALOG_RESULT. This copies the TV host file over the existing `ssh TV_HOST` key
when (and only when) its generated_at changed, so feedback is applied once per
overnight run. Never fails the caller: any problem is logged and exits 0.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REMOTE = os.environ.get("WDBS_RESULT_REMOTE", "TV_HOST:FieldStation42/runtime/wdbs_channel10_catalog_result.json")
LOCAL = Path(os.environ.get("WDBS_CATALOG_RESULT",
                            "/srv/example-archive/MediaAutomation/wdbs/control/channel10_catalog_result.json"))


def log(msg):
    print(f"wdbs-result-pull: {msg}", flush=True)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "result.json"
        r = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", REMOTE, str(tmp)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            log(f"could not fetch {REMOTE}: {r.stderr.strip()[:200]}")
            return 0
        try:
            remote = json.loads(tmp.read_text())
            assert remote.get("schema") == 1 and isinstance(remote.get("programs"), list)
        except Exception as exc:
            log(f"remote result is not a valid schema-1 result ({exc}); ignoring")
            return 0
        if LOCAL.exists():
            try:
                if json.loads(LOCAL.read_text()).get("generated_at") == remote.get("generated_at"):
                    log(f"unchanged (generated_at {remote.get('generated_at')})")
                    return 0
            except Exception:
                pass
        remote["feedback_applied"] = False
        LOCAL.parent.mkdir(parents=True, exist_ok=True)
        staging = LOCAL.with_suffix(".pull.tmp")
        staging.write_text(json.dumps(remote, indent=2) + "\n")
        os.replace(staging, LOCAL)
        log(f"pulled result generated_at {remote.get('generated_at')} "
            f"({len(remote['programs'])} programs) -> {LOCAL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
