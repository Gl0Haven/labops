import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from labops.db import connect, migrate
from labops.spool import drain, write_event
from labops.tasks import add_repo
from tests.helpers import ROOT, make_repo

PYTHON = Path(os.environ.get("LABOPS_TEST_PYTHON") or sys.executable)
SRC = ROOT / "src"


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-spool-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / ".test-instance").touch()
        self.env_patch = patch.dict(os.environ, {
            "LABOPS_HOME": str(self.home),
            "LABOPS_FAKE_NOW": "2026-10-19T12:00:00Z",
        })
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.temp.cleanup()

    def test_same_payload_same_second_has_one_atomic_event_and_only_metadata(self):
        raw = json.dumps({"hook_event_name": "Stop", "session_id": "s1", "turn_id": "t1", "cwd": "C:/toy",
                          "transcript": "private", "tool_input": {"secret": "private"}}).encode()
        with patch("labops.spool.now", return_value="2026-10-19T12:00:00Z"):
            first = write_event("codex", raw)
            second = write_event("codex", raw)
        self.assertEqual(first, second)
        self.assertEqual(len(list((self.home / "spool" / "new").glob("*.json"))), 1)
        event = json.loads(first.read_text(encoding="utf-8"))
        self.assertEqual(set(event), {"client", "event_type", "session_id", "turn_id", "cwd", "ts", "payload_hash"})
        self.assertEqual(event["event_type"], "Stop")
        self.assertNotIn("transcript", event)
        self.assertNotIn("tool_input", event)

    def test_invalid_json_is_recorded_as_metadata(self):
        with patch("labops.spool.now", return_value="2026-10-19T12:00:00Z"):
            path = write_event("claude", b"{not-json")
        event = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(event["event_type"], "invalid_json")
        self.assertEqual(event["client"], "claude")
        conn = connect(self.home / "labops.db")
        try:
            migrate(conn)
            result = drain(conn)
            self.assertEqual((result["ingested"], result["invalid"]), (1, 1))
        finally:
            conn.close()

    def test_overflow_drops_new_event_and_logs_warning(self):
        new_dir = self.home / "spool" / "new"
        new_dir.mkdir(parents=True)
        for index in range(10000):
            (new_dir / f"existing-{index:05}.json").touch()
        with patch("labops.spool.now", return_value="2026-10-19T12:00:00Z"):
            path = write_event("codex", b'{"hook_event_name":"Stop"}')
        self.assertFalse(path.exists())
        self.assertIn("reached 10000 files", (self.home / "spool" / "overflow.log").read_text(encoding="utf-8"))

    def test_drain_is_idempotent_and_maps_cwd_once_per_repo(self):
        repo = self.root / "repo"
        make_repo(repo)
        conn = connect(self.home / "labops.db")
        migrate(conn)
        add_repo(conn, repo_id="toy", path=repo)
        try:
            with patch("labops.spool.now", return_value="2026-10-19T12:00:00Z"):
                one = write_event("codex", json.dumps({"hook_event_name": "Stop", "cwd": str(repo), "turn_id": "1"}).encode())
                write_event("codex", json.dumps({"hook_event_name": "SessionEnd", "cwd": str(repo), "turn_id": "2"}).encode())
            import labops.reconcile
            with patch.object(labops.reconcile, "reconcile", return_value={"unattributed_commits": 0}) as reconcile_mock:
                first = drain(conn)
            self.assertEqual(first["ingested"], 2)
            self.assertEqual(first["repos_reconciled"], 1)
            reconcile_mock.assert_called_once_with(conn, "toy")
            duplicate_path = self.home / "spool" / "done" / one.name
            replay = self.home / "spool" / "new" / one.name
            replay.write_bytes(duplicate_path.read_bytes())
            second = drain(conn)
            self.assertEqual(second["duplicates"], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM spool_events").fetchone()[0], 2)
        finally:
            conn.close()

    def test_two_process_drains_serialize_and_each_exit_zero(self):
        with patch("labops.spool.now", return_value="2026-10-19T12:00:00Z"):
            write_event("codex", b'{"hook_event_name":"Stop","turn_id":"concurrent"}')
        conn = connect(self.home / "labops.db")
        migrate(conn)
        conn.close()
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["LABOPS_HOME"] = str(self.home)
        code = (
            "import json,time\n"
            "import labops.spool as spool\n"
            "from labops.db import connect,migrate\n"
            "original=spool._drain_locked\n"
            "def slow(conn):\n    time.sleep(0.15)\n    return original(conn)\n"
            "spool._drain_locked=slow\n"
            "conn=connect(); migrate(conn); print(json.dumps(spool.drain(conn))); conn.close()\n"
        )
        processes = [subprocess.Popen([str(PYTHON), "-c", code], cwd=SRC, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=env, text=True)
                     for _ in range(2)]
        outputs = [process.communicate(timeout=20) for process in processes]
        self.assertEqual([process.returncode for process in processes], [0, 0], [item[1] for item in outputs])
        summaries = [json.loads(stdout) for stdout, _ in outputs]
        self.assertEqual(sum(item["ingested"] for item in summaries), 1)
        verify = connect(self.home / "labops.db")
        try:
            self.assertEqual(verify.execute("SELECT COUNT(*) FROM spool_events").fetchone()[0], 1)
        finally:
            verify.close()

    def test_hook_import_does_not_load_sqlite_mcp_or_cli(self):
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["LABOPS_HOME"] = str(self.home)
        result = subprocess.run(
            [str(PYTHON), "-c", "import sys,labops.hook; assert 'sqlite3' not in sys.modules; assert 'mcp' not in sys.modules; assert 'labops.cli' not in sys.modules"],
            cwd=SRC, capture_output=True, env=env, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fifty_hook_subprocesses_have_sub_400ms_p95(self):
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["LABOPS_HOME"] = str(self.home)
        env["LABOPS_FAKE_NOW"] = "2026-10-19T12:00:00Z"
        payload = b'{"hook_event_name":"Stop","session_id":"timing","cwd":"C:/timing"}'
        durations = []
        for _ in range(50):
            started = time.perf_counter()
            result = subprocess.run([str(PYTHON), "-m", "labops.hook", "ingest", "--client", "codex"],
                                    input=payload, cwd=SRC, capture_output=True, env=env, timeout=5)
            durations.append((time.perf_counter() - started) * 1000)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        ordered = sorted(durations)
        p50 = statistics.median(ordered)
        p95 = ordered[47]
        self.assertLess(p95, 400, f"p50={p50:.2f}ms p95={p95:.2f}ms")


if __name__ == "__main__":
    unittest.main()
