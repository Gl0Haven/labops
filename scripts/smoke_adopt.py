"""Run adopted-process smoke scenarios with temporary databases and mocked process probes."""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.test_adopt import AdoptedRunsTests  # noqa: E402


def _run_case(method: str) -> bool:
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(
        unittest.TestSuite([AdoptedRunsTests(method)])
    )
    if not result.wasSuccessful():
        sys.stderr.write(output.getvalue())
    return result.wasSuccessful()


def main() -> int:
    scenarios = {
        "adopt_ok": "test_adopt_stores_process_identity_and_is_idempotent",
        "secretary_denied": "test_process_not_found_and_secretary_are_rejected_without_extra_query",
        "idempotent": "test_adopt_stores_process_identity_and_is_idempotent",
        "pid_reuse_detected": "test_pid_reuse_marks_run_exited_and_creates_digest_event",
        "exit_event_digest": "test_pid_reuse_marks_run_exited_and_creates_digest_event",
        "queue_idle_after_exit": "test_queue_becomes_idle_when_adopted_process_exits",
        "watch_glob_progress": "test_watch_globs_feed_progress_and_mtime",
    }
    checks = {name: _run_case(method) for name, method in scenarios.items()}
    result = {"ok": all(checks.values()), **checks}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
