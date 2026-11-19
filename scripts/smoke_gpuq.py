"""Run the GPU queue smoke scenarios with temporary state and mocked process starts."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.test_gpuq import GpuQueueTests  # noqa: E402
from tests.test_runs import RunsTests  # noqa: E402


def _run_case(test_case, method: str) -> bool:
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(
        unittest.TestSuite([test_case(method)])
    )
    if not result.wasSuccessful():
        sys.stderr.write(output.getvalue())
    return result.wasSuccessful()


def main() -> int:
    scenarios = {
        "ask_once": (GpuQueueTests, "test_busy_queue_asks_once_and_manual_idle_once_includes_failed_previous"),
        "manual_idle_once": (GpuQueueTests, "test_busy_queue_asks_once_and_manual_idle_once_includes_failed_previous"),
        "auto_handoff": (GpuQueueTests, "test_auto_handoff_stays_launching_and_only_hands_off_once"),
        "auto_launcher_mock": (GpuQueueTests, "test_launcher_mode_is_mocked_and_failed_predecessor_does_not_block"),
        "hash_guard": (GpuQueueTests, "test_launcher_hash_guard_fails_before_invocation"),
        "manual_dequeue": (GpuQueueTests, "test_matching_manual_run_dequeues_queue_item"),
        "fifo_single_launch": (GpuQueueTests, "test_fifo_launches_at_most_one_item_per_tick"),
        "secretary_register_denied": (GpuQueueTests, "test_cancel_and_secretary_register_permissions"),
        "watch_glob_mtime": (RunsTests, "test_watch_glob_mtime_extends_run_progress_time"),
        "orphan_not_busy": (GpuQueueTests, "test_orphaned_process_is_not_gpu_busy_and_event_is_reportable_once"),
    }
    checks = {name: _run_case(test_case, method) for name, (test_case, method) in scenarios.items()}
    result = {"ok": all(checks.values()), **checks}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
