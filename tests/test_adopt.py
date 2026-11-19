import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from labops import gpuq
from labops import cli
from labops.cli import build_parser
from labops.db import connect, migrate
from labops.errors import ValidationError
from labops.gpuq import queue_choose, queue_list, queue_register, queue_tick
from labops.notify import (_event_notification, _urgent_kinds, merge_pending, notify_config,
                           scan as notify_scan)
from labops.progress import progress_summary
from labops.redact import for_actor
from labops.runs import (ProcessInfoUnavailable, _process_info, _scan_adopted, adopt_process,
                         list_runs, scan_runs)


class AdoptedRunsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-adopt-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "toy-repo"
        self.repo.mkdir()
        self.logs = self.repo / "logs"
        self.logs.mkdir()
        self.log_path = self.logs / "train.log"
        self.log_path.write_text("step 1/20\n", encoding="utf-8")
        self.entrypoint = self.repo / "scripts" / "train.ps1"
        self.entrypoint.parent.mkdir()
        self.entrypoint.write_text("Write-Output 'toy'\n", encoding="utf-8")
        self.conn = connect(self.root / "home" / "labops.db")
        migrate(self.conn)
        self._add_repo("toy", self.repo)
        self.started_at = "2026-10-21T04:00:00.1250000Z"
        self.process = {
            "id": 45678,
            "start_utc": self.started_at,
            "command_line": f'"C:\\Python\\python.exe" "{self.entrypoint}" --config toy.json',
        }

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _add_repo(self, repo_id, path, confidential=False):
        self.conn.execute(
            "INSERT INTO repos(repo_id,path,confidential,created_at) VALUES(?,?,?,?)",
            (repo_id, str(path), int(confidential), "2026-10-21T00:00:00Z"),
        )

    @staticmethod
    def _ts(minutes=0):
        return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")

    def _adopt(self, **kwargs):
        defaults = {
            "repo_id": "toy", "pid": self.process["id"], "title": "toy training",
            "log_path": str(self.log_path), "watch_globs": ["metrics/**/*.jsonl"], "actor": "codex",
        }
        defaults.update(kwargs)
        with patch("labops.runs._process_info", return_value=self.process):
            return adopt_process(self.conn, **defaults)

    def test_adopt_stores_process_identity_and_is_idempotent(self):
        first = self._adopt()
        second = self._adopt()
        row = first["run"]
        self.assertEqual(first["replayed"], False)
        self.assertEqual(second["replayed"], True)
        self.assertEqual(second["run"]["run_key"], row["run_key"])
        self.assertEqual(row["source"], "adopted")
        self.assertEqual(row["title"], "toy training")
        self.assertEqual(row["pid"], self.process["id"])
        self.assertEqual(row["started_at"], row["process_started_at"])
        self.assertEqual(row["process_started_at"], "2026-10-21T04:00:00.125000Z")
        expected_epoch = int(datetime.fromisoformat(row["process_started_at"].replace("Z", "+00:00")).timestamp())
        self.assertEqual(row["run_key"], f"adopt_{self.process['id']}_{expected_epoch}")
        self.assertEqual(row["script_path"], str(self.entrypoint))
        self.assertEqual(row["run_dir"], str(self.log_path.parent))
        self.assertEqual(row["log_path"], str(self.log_path.resolve()))
        self.assertEqual(json.loads(row["watch_globs"]), ["metrics/**/*.jsonl"])
        self.assertEqual(len(row["command_line"]), len(self.process["command_line"]))
        events = self.conn.execute(
            "SELECT * FROM events WHERE entity_type='bg_run' AND entity_id=? AND to_state='run_started'",
            (row["run_key"],),
        ).fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]["detail_json"])["source"], "adopted")
        self.assertEqual(len(list_runs(self.conn, "toy")), 1)

    def test_process_not_found_and_secretary_are_rejected_without_extra_query(self):
        with patch("labops.runs._process_info") as probe:
            with self.assertRaisesRegex(ValidationError, "adopt_actor_not_allowed"):
                adopt_process(self.conn, repo_id="toy", pid=123, title="no", actor="secretary")
            probe.assert_not_called()
        with patch("labops.runs._process_info", return_value=None):
            with self.assertRaisesRegex(ValidationError, "process_not_found"):
                adopt_process(self.conn, repo_id="toy", pid=123, title="missing", actor="opus")

    def test_log_path_must_be_an_existing_file_inside_repo(self):
        outside = self.root / "outside.log"
        outside.write_text("outside", encoding="utf-8")
        with patch("labops.runs._process_info", return_value=self.process):
            with self.assertRaisesRegex(ValidationError, "adopt_log_path_outside_repo"):
                adopt_process(self.conn, repo_id="toy", pid=self.process["id"], title="bad path",
                              log_path=str(outside), actor="codex")
            with self.assertRaisesRegex(ValidationError, "adopt_log_path_missing"):
                adopt_process(self.conn, repo_id="toy", pid=self.process["id"], title="missing path",
                              log_path=str(self.repo / "missing.log"), actor="codex")

    def test_pid_reuse_marks_run_exited_and_creates_digest_event(self):
        adopted = self._adopt()["run"]
        reused = {**self.process, "start_utc": "2026-10-21T04:00:04.125000Z"}
        result = _scan_adopted(self.conn, self._ts(), process_info=lambda _pid: reused)
        row = self.conn.execute("SELECT status,finished_at,exit_code FROM bg_runs WHERE run_key=?",
                                (adopted["run_key"],)).fetchone()
        self.assertEqual(result["events"], 1)
        self.assertEqual(row["status"], "exited")
        self.assertIsNotNone(row["finished_at"])
        self.assertIsNone(row["exit_code"])
        event = self.conn.execute(
            "SELECT * FROM events WHERE entity_id=? AND to_state='run_exited'", (adopted["run_key"],)
        ).fetchone()
        message = _event_notification(self.conn, event)
        self.assertEqual(message["kind"], "run_exited")
        self.assertIn("已结束（退出码未知）", message["text"])
        self.assertNotIn("run_exited", _urgent_kinds(self.conn))
        notify_config(self.conn, digest_minutes=0)
        notify_scan(self.conn)
        merge_pending(self.conn, now_ts=self._ts(1))
        digest = self.conn.execute("SELECT text FROM notifications WHERE kind='batch_digest'").fetchone()
        self.assertIsNotNone(digest)
        self.assertIn("已结束（退出码未知）", digest["text"])

    def test_process_query_unavailable_does_not_mark_run_exited(self):
        adopted = self._adopt()["run"]

        def unavailable(_pid):
            raise ProcessInfoUnavailable("process_query_timeout")

        result = _scan_adopted(self.conn, self._ts(), process_info=unavailable)
        status = self.conn.execute("SELECT status FROM bg_runs WHERE run_key=?",
                                   (adopted["run_key"],)).fetchone()[0]
        self.assertEqual(result["events"], 0)
        self.assertEqual(status, "running")

    def test_watch_globs_feed_progress_and_mtime(self):
        metrics = self.repo / "metrics" / "nested" / "progress.jsonl"
        metrics.parent.mkdir(parents=True)
        metrics.write_text('{"epoch":"epoch 8/20"}\n', encoding="utf-8")
        repo_metrics = self.repo / "repo_metrics" / "latest.jsonl"
        repo_metrics.parent.mkdir(parents=True)
        repo_metrics.write_text('{"epoch":"epoch 9/20"}\n', encoding="utf-8")
        self.conn.execute("UPDATE repos SET run_watch_globs=? WHERE repo_id='toy'",
                          (json.dumps(["repo_metrics/**/*.jsonl"]),))
        now_ts = datetime.now(timezone.utc)
        old_log = (now_ts - timedelta(minutes=10)).timestamp()
        run_glob_mtime = (now_ts - timedelta(minutes=2)).timestamp()
        latest = (now_ts - timedelta(seconds=5)).timestamp()
        os.utime(self.log_path, (old_log, old_log))
        os.utime(metrics, (run_glob_mtime, run_glob_mtime))
        os.utime(repo_metrics, (latest, latest))
        adopted = self._adopt()["run"]

        _scan_adopted(self.conn, self._ts(), process_info=lambda _pid: self.process)

        row = self.conn.execute("SELECT log_mtime,progress_hint,progress_at FROM bg_runs WHERE run_key=?",
                                (adopted["run_key"],)).fetchone()
        self.assertEqual(row["progress_hint"], '{"epoch":"epoch 9/20"}')
        self.assertAlmostEqual(datetime.fromisoformat(row["log_mtime"].replace("Z", "+00:00")).timestamp(),
                               latest, delta=0.1)
        self.assertIsNotNone(row["progress_at"])

    def test_queue_becomes_idle_when_adopted_process_exits(self):
        adopted = self._adopt()["run"]
        with patch.object(gpuq, "_ALLOWED_ENTRY_ROOTS", (self.root,)):
            item = queue_register(self.conn, repo_id="toy", title="after adopted run",
                                  entrypoint=str(self.entrypoint), context="synthetic test")["queue_item"]

        with patch("labops.runs._process_info", return_value=self.process):
            queue_tick(self.conn, self._ts(), pid_alive=lambda _pid: False)
            queue_choose(self.conn, queue_id=item["queue_id"], choice="manual", actor="secretary",
                         now_ts=self._ts(1))
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key='gpuq_last_busy'").fetchone()[0], "1")

        scanned = scan_runs(self.conn, state_root=self.root / "empty-background-state",
                            now_ts=self._ts(2), process_info=lambda _pid: None)
        idle = queue_tick(self.conn, self._ts(3), pid_alive=lambda _pid: False)
        self.assertEqual(scanned["events"], 1)
        self.assertEqual(idle["manual_idle_notified"], 1)
        self.assertEqual(self.conn.execute("SELECT status FROM bg_runs WHERE run_key=?",
                                           (adopted["run_key"],)).fetchone()[0], "exited")

    def test_secretary_progress_view_redacts_adopted_identity(self):
        private_repo = self.root / "private"
        private_repo.mkdir()
        self._add_repo("private", private_repo, confidential=True)
        info = {**self.process, "id": 45679}
        with patch("labops.runs._process_info", return_value=info):
            adopt_process(self.conn, repo_id="private", pid=info["id"], title="private title",
                          actor="opus")
        summary = progress_summary(self.conn)
        private_raw = next(run for run in summary["runs"] if run.get("pid") == info["id"])
        self.assertEqual((private_raw["source"], private_raw["title"]), ("adopted", "private title"))
        safe = for_actor(self.conn, "secretary", summary)
        private = next(run for run in safe["runs"] if run.get("status") == "running")
        self.assertNotIn("source", private)
        self.assertNotIn("title", private)
        self.assertNotIn("private title", json.dumps(safe, ensure_ascii=False))
        with patch("labops.runs._process_info", return_value=info):
            safe_queue = for_actor(self.conn, "secretary",
                                   queue_list(self.conn, pid_alive=lambda _pid: False))
        self.assertEqual(safe_queue["gpu"]["runs"], [])

    def test_cli_runs_adopt_dispatches_with_repeated_watch_globs(self):
        cli_conn = connect(self.root / "cli-home" / "labops.db")
        migrate(cli_conn)
        cli_conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES(?,?,?)",
                         ("toy", str(self.repo), "2026-10-21T00:00:00Z"))
        args = build_parser().parse_args([
            "runs", "adopt", "--repo-id", "toy", "--pid", str(self.process["id"]),
            "--title", "cli adopted", "--log-path", "logs/train.log",
            "--watch", "metrics/**/*.jsonl", "--watch", "progress/*.txt",
        ])
        with patch("labops.cli._db", return_value=cli_conn), \
             patch("labops.runs._process_info", return_value=self.process):
            result = cli.dispatch(args)
        self.assertEqual(result["run"]["title"], "cli adopted")
        self.assertEqual(json.loads(result["run"]["watch_globs"]),
                         ["metrics/**/*.jsonl", "progress/*.txt"])

    def test_queue_list_exposes_adopted_source_and_title(self):
        adopted = self._adopt()["run"]
        with patch("labops.runs._process_info", return_value=self.process):
            result = queue_list(self.conn, pid_alive=lambda _pid: False)
        self.assertTrue(result["gpu"]["busy"])
        self.assertEqual(result["gpu"]["runs"][0]["source"], "adopted")
        self.assertEqual(result["gpu"]["runs"][0]["title"], adopted["title"])

    def test_process_info_parses_json_and_uses_timeout(self):
        completed = type("Result", (), {
            "returncode": 0, "stdout": json.dumps(self.process), "stderr": "",
        })()
        with patch("labops.runs.shutil.which", return_value="pwsh.exe"), \
             patch("labops.runs.subprocess.run", return_value=completed) as run:
            result = _process_info(self.process["id"])
        self.assertEqual(result, self.process)
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_process_info_timeout_is_not_misreported_as_process_exit(self):
        with patch("labops.runs.shutil.which", return_value="pwsh.exe"), \
             patch("labops.runs.subprocess.run", side_effect=subprocess.TimeoutExpired("pwsh", 15)):
            with self.assertRaisesRegex(ProcessInfoUnavailable, "process_query_timeout"):
                _process_info(self.process["id"])

    def test_process_info_script_runs_with_stubbed_process_cmdlets(self):
        pwsh = shutil.which("pwsh")
        self.assertIsNotNone(pwsh)
        real_run = subprocess.run

        def stubbed_run(command, **kwargs):
            stub = (
                "function Get-Process { param([int]$Id) "
                "[pscustomobject]@{ Id=$Id; StartTime=[datetime]::Parse('2026-10-21T04:00:00Z') } }\n"
                "function Get-CimInstance { param($ClassName,$Filter) "
                "[pscustomobject]@{ CommandLine='stubbed command line' } }\n"
            )
            return real_run([*command[:3], stub + command[3]], **kwargs)

        with patch("labops.runs.shutil.which", return_value=pwsh), \
             patch("labops.runs.subprocess.run", side_effect=stubbed_run):
            result = _process_info(self.process["id"])
        self.assertEqual(result["id"], self.process["id"])
        self.assertEqual(result["command_line"], "stubbed command line")


if __name__ == "__main__":
    unittest.main()
