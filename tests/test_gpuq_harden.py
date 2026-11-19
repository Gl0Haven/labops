import hashlib
import json
import multiprocessing
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from labops import cli, gpuq
from labops.db import connect, migrate
from labops.errors import ValidationError
from labops.gpuq import (_fingerprint, _fingerprint_changes, queue_choose, queue_register,
                         queue_tick)
from labops.notify import _event_notification
from labops.runs import list_runs
from tests.gpuq_harden_support import run_race_scenario
from tests.helpers import commit_file, make_repo


class GPUQHardenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-gpuq-harden-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "toy-repo"
        make_repo(self.repo, files={
            "scripts/train.ps1": "Write-Output 'toy launcher'\n",
            "config.yaml": "batch_size: 8\n",
            "train.py": "print('toy train')\n",
        })
        self.entrypoint = self.repo / "scripts" / "train.ps1"
        self.conn = connect(self.root / "home" / "labops.db")
        migrate(self.conn)
        self.conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy',?,?)",
                          (str(self.repo), "2026-10-21T09:00:00Z"))

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    @staticmethod
    def _ts(minutes=0):
        return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")

    def _register(self, title="GPU hardening", **kwargs):
        with patch.object(gpuq, "_ALLOWED_ENTRY_ROOTS", (self.root,)):
            return queue_register(
                self.conn, repo_id=kwargs.pop("repo_id", "toy"), title=title,
                entrypoint=kwargs.pop("entrypoint", str(self.entrypoint)),
                context=kwargs.pop("context", "temporary test fixture"), **kwargs,
            )["queue_item"]

    def _add_run(self, run_key, *, script_path=None, status="running", pid=1234, started_at=None,
                 finished_at=None, exit_code=None):
        self.conn.execute(
            "INSERT INTO bg_runs(run_key,repo_id,run_dir,script_path,status,pid,started_at,finished_at,"
            "exit_code,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_key, "toy", str(self.root / "runs" / run_key[:12]),
             str(script_path or (self.repo / "other.ps1")), status, pid,
             started_at or self._ts(-5), finished_at, exit_code, self._ts()),
        )

    def _event_details(self, queue_id, kind):
        rows = self.conn.execute(
            "SELECT detail_json FROM events WHERE entity_type='gpu_queue' AND entity_id=? ORDER BY seq",
            (queue_id,),
        ).fetchall()
        return [json.loads(row[0]) for row in rows if json.loads(row[0]).get("notification_kind") == kind]

    def _set_launch_mode(self, mode):
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES('queue_launch_mode',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (mode,),
        )

    def test_fingerprint_covers_tracked_changes_untracked_count_and_configs_only(self):
        baseline = _fingerprint(self.repo, ["config.yaml"])
        self.assertTrue(baseline["git_head"])
        self.assertEqual(baseline["git_dirty"], False)
        self.assertEqual(baseline["untracked_count"], 0)
        self.assertEqual(baseline["configs"][0]["sha256"],
                         hashlib.sha256((self.repo / "config.yaml").read_bytes()).hexdigest())

        (self.repo / "train.py").write_text("print('changed tracked source')\n", encoding="utf-8")
        (self.repo / "config.yaml").write_text("batch_size: 16\n", encoding="utf-8")
        untracked = self.repo / "untracked.bin"
        untracked.write_bytes(b"must only be counted")
        reads = []
        original_read = Path.read_bytes

        def observe_read(path):
            reads.append(path.resolve())
            return original_read(path)

        with patch.object(Path, "read_bytes", observe_read):
            changed = _fingerprint(self.repo, ["config.yaml"])

        self.assertTrue(changed["git_dirty"])
        self.assertEqual(changed["untracked_count"], 1)
        self.assertNotEqual(changed["git_diff_sha256"], baseline["git_diff_sha256"])
        self.assertNotEqual(changed["configs"][0]["sha256"], baseline["configs"][0]["sha256"])
        self.assertEqual(reads, [(self.repo / "config.yaml").resolve()])
        self.assertIn("git_dirty", _fingerprint_changes(baseline, changed))
        self.assertIn("config.yaml", _fingerprint_changes(baseline, changed))

    def test_fingerprint_git_failure_sets_null_fields_and_records_errors(self):
        with patch("labops.gpuq.subprocess.run", side_effect=FileNotFoundError("git missing")):
            value = _fingerprint(self.repo, ["config.yaml", "missing.yaml"])
        self.assertIsNone(value["git_head"])
        self.assertIsNone(value["git_dirty"])
        self.assertIsNone(value["git_diff_sha256"])
        self.assertIsNone(value["untracked_count"])
        self.assertTrue({"git_head", "git_dirty", "git_diff_sha256", "untracked_count"}.issubset(value["errors"]))
        self.assertIsNotNone(value["configs"][0]["sha256"])
        self.assertIsNone(value["configs"][1]["sha256"])

    def test_config_paths_validate_persist_and_are_exposed_only_by_cli(self):
        row = self._register(config_paths=["config.yaml", "config.yaml"])
        self.assertEqual(json.loads(row["config_paths"]), ["config.yaml"])
        registered = json.loads(row["register_fingerprint"])
        self.assertEqual(registered["configs"][0]["path"], "config.yaml")
        for config_paths in (["../outside.yaml"], [str(self.repo / "config.yaml")], ["missing.yaml"]):
            with self.subTest(config_paths=config_paths):
                with self.assertRaises(ValidationError):
                    self._register(config_paths=config_paths)
        with self.assertRaisesRegex(ValidationError, "too_many_queue_config_paths"):
            self._register(config_paths=["config.yaml"] * 11)

        args = cli.build_parser().parse_args([
            "queue", "register", "--repo-id", "toy", "--title", "cli config",
            "--entrypoint", str(self.entrypoint), "--context", "toy", "--config", "config.yaml",
        ])
        self.assertEqual(args.config, ["config.yaml"])
        cli_conn = connect(self.root / "cli-home" / "labops.db")
        migrate(cli_conn)
        cli_conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy',?,?)",
                         (str(self.repo), "t"))
        with patch("labops.cli._db", return_value=cli_conn), \
             patch.object(gpuq, "_ALLOWED_ENTRY_ROOTS", (self.root,)):
            result = cli.dispatch(args)
        self.assertEqual(json.loads(result["queue_item"]["config_paths"]), ["config.yaml"])

    def test_queue_choose_rechecks_gpu_snapshot_inside_atomic_claim(self):
        queue = self._register()
        queue_tick(self.conn, self._ts(), pid_alive=lambda _pid: False)
        real_gpu_busy = gpuq.gpu_busy

        def add_run_after_snapshot(*args, **kwargs):
            result = real_gpu_busy(*args, **kwargs)
            self._add_run("a" * 64, status="running", pid=101)
            return result

        with patch.object(gpuq, "gpu_busy", side_effect=add_run_after_snapshot):
            with self.assertRaisesRegex(ValidationError, "gpu_state_changed"):
                queue_choose(self.conn, queue_id=queue["queue_id"], choice="now", actor="secretary",
                             pid_alive=lambda _pid: False, now_ts=self._ts(1))
        self.assertEqual(self.conn.execute("SELECT status FROM gpu_queue WHERE queue_id=?",
                                           (queue["queue_id"],)).fetchone()[0], "ASKED")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM gpu_queue WHERE status='LAUNCHING'").fetchone()[0], 0)

    def test_queue_tick_counts_gpu_state_contention_without_starting_auto_item(self):
        queue = self._register()
        queue_tick(self.conn, self._ts(), pid_alive=lambda _pid: False)
        queue_choose(self.conn, queue_id=queue["queue_id"], choice="auto", actor="secretary",
                     pid_alive=lambda _pid: False, now_ts=self._ts(1))
        real_fingerprint = gpuq._fingerprint

        def add_run_after_snapshot(repo_path, config_paths):
            self._add_run("b" * 64, status="running", pid=102)
            return real_fingerprint(repo_path, config_paths)

        with patch.object(gpuq, "_fingerprint", side_effect=add_run_after_snapshot):
            result = queue_tick(self.conn, self._ts(2), pid_alive=lambda _pid: False)
        self.assertEqual(result["launch_contended"], 1)
        self.assertEqual(self.conn.execute("SELECT status FROM gpu_queue WHERE queue_id=?",
                                           (queue["queue_id"],)).fetchone()[0], "WAIT_AUTO")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM gpu_queue WHERE status='LAUNCHING'").fetchone()[0], 0)

    def test_changed_fingerprint_does_not_block_launch_and_is_copied_to_run(self):
        queue = self._register(config_paths=["config.yaml"])
        registered = json.loads(queue["register_fingerprint"])
        commit_file(self.repo, "train.py", "print('committed after registration')\n", "move HEAD")
        (self.repo / "config.yaml").write_text("batch_size: 32\n", encoding="utf-8")
        self._set_launch_mode("launcher")
        queue_tick(self.conn, self._ts(), pid_alive=lambda _pid: False)
        queue_choose(self.conn, queue_id=queue["queue_id"], choice="now", actor="secretary",
                     pid_alive=lambda _pid: False, now_ts=self._ts(1))
        calls = []
        launch_result = queue_tick(
            self.conn, self._ts(2), pid_alive=lambda _pid: False,
            launcher=lambda _row, _run_dir: calls.append(True) or {"ok": True},
        )
        self.assertEqual(launch_result["launching"], 1)
        self.assertEqual(launch_result["launch_failed"], 0)
        self.assertEqual(calls, [True])
        launched_row = self.conn.execute("SELECT launch_fingerprint FROM gpu_queue WHERE queue_id=?",
                                         (queue["queue_id"],)).fetchone()
        launched_fingerprint = json.loads(launched_row["launch_fingerprint"])
        self.assertNotEqual(registered["git_head"], launched_fingerprint["git_head"])
        self.assertTrue(launched_fingerprint["git_dirty"])

        run_key = "c" * 64
        self._add_run(run_key, script_path=self.entrypoint, status="completed", pid=0,
                      started_at=self._ts(3), finished_at=self._ts(4), exit_code=0)
        matched = queue_tick(self.conn, self._ts(5), pid_alive=lambda _pid: False)
        self.assertEqual(matched["matched"], 1)
        run = self.conn.execute("SELECT fingerprint_json FROM bg_runs WHERE run_key=?", (run_key,)).fetchone()
        self.assertEqual(json.loads(run["fingerprint_json"]), launched_fingerprint)
        listed = next(item for item in list_runs(self.conn, "toy") if item["run_key"] == run_key)
        self.assertEqual(listed["fingerprint_json"], run["fingerprint_json"])
        event = self.conn.execute(
            "SELECT * FROM events WHERE entity_type='gpu_queue' AND entity_id=? "
            "AND to_state='LAUNCHED' ORDER BY seq DESC LIMIT 1", (queue["queue_id"],),
        ).fetchone()
        detail = json.loads(event["detail_json"])
        self.assertIn("git_head", detail["fingerprint_changed"])
        self.assertIn("config.yaml", detail["fingerprint_changed"])
        notification = _event_notification(self.conn, event)
        self.assertIn("代码与登记时不同", notification["text"])
        self.assertIn("config.yaml 已变", notification["text"])

    def test_manual_idle_notice_includes_config_fingerprint_change(self):
        queue = self._register(config_paths=["config.yaml"])
        self._add_run("d" * 64, status="running", pid=222, script_path=self.repo / "other.ps1")
        alive = lambda pid: pid == 222
        queue_tick(self.conn, self._ts(), pid_alive=alive)
        queue_choose(self.conn, queue_id=queue["queue_id"], choice="manual", actor="secretary",
                     pid_alive=alive, now_ts=self._ts(1))
        (self.repo / "config.yaml").write_text("batch_size: 64\n", encoding="utf-8")
        self.conn.execute("UPDATE bg_runs SET status='failed',finished_at=?,exit_code=2 WHERE run_key=?",
                          (self._ts(2), "d" * 64))

        queue_tick(self.conn, self._ts(3), pid_alive=alive)

        notices = self._event_details(queue["queue_id"], "queue_idle_manual")
        self.assertEqual(len(notices), 1)
        self.assertIn("config.yaml", notices[0]["fingerprint_changed"])
        self.assertIn("config.yaml 已变", notices[0]["fingerprint_summary"])
        event = self.conn.execute(
            "SELECT * FROM events WHERE entity_type='gpu_queue' AND entity_id=? AND to_state='WAIT_MANUAL' "
            "ORDER BY seq DESC LIMIT 1", (queue["queue_id"],),
        ).fetchone()
        notification = _event_notification(self.conn, event)
        self.assertIn("config.yaml 已变", notification["text"])

    def test_auto_handoff_captures_and_reports_launch_fingerprint(self):
        queue = self._register(config_paths=["config.yaml"])
        (self.repo / "config.yaml").write_text("batch_size: 256\n", encoding="utf-8")
        queue_tick(self.conn, self._ts(), pid_alive=lambda _pid: False)
        queue_choose(self.conn, queue_id=queue["queue_id"], choice="auto", actor="secretary",
                     pid_alive=lambda _pid: False, now_ts=self._ts(1))

        result = queue_tick(self.conn, self._ts(2), pid_alive=lambda _pid: False)

        self.assertEqual(result["handed_off"], 1)
        row = self.conn.execute("SELECT launch_fingerprint FROM gpu_queue WHERE queue_id=?",
                                (queue["queue_id"],)).fetchone()
        self.assertIsNotNone(row["launch_fingerprint"])
        notices = self._event_details(queue["queue_id"], "queue_idle_manual")
        self.assertEqual(len(notices), 1)
        self.assertTrue(notices[0]["handoff"])
        self.assertIn("config.yaml", notices[0]["fingerprint_changed"])
        event = self.conn.execute(
            "SELECT * FROM events WHERE entity_type='gpu_queue' AND entity_id=? "
            "AND to_state='LAUNCHING' ORDER BY seq DESC LIMIT 1", (queue["queue_id"],),
        ).fetchone()
        notification = _event_notification(self.conn, event)
        self.assertIn("config.yaml 已变", notification["text"])

    def test_manual_run_matching_computes_fingerprint_when_no_launch_was_claimed(self):
        queue = self._register(config_paths=["config.yaml"])
        (self.repo / "config.yaml").write_text("batch_size: 128\n", encoding="utf-8")
        run_key = "e" * 64
        self._add_run(run_key, script_path=self.entrypoint, status="completed", pid=0,
                      started_at=self._ts(1), finished_at=self._ts(2), exit_code=0)

        result = queue_tick(self.conn, self._ts(3), pid_alive=lambda _pid: False)

        self.assertEqual(result["matched"], 1)
        item = self.conn.execute("SELECT status,launch_fingerprint FROM gpu_queue WHERE queue_id=?",
                                 (queue["queue_id"],)).fetchone()
        run = self.conn.execute("SELECT fingerprint_json FROM bg_runs WHERE run_key=?", (run_key,)).fetchone()
        self.assertEqual(item["status"], "LAUNCHED")
        self.assertEqual(json.loads(item["launch_fingerprint"]), json.loads(run["fingerprint_json"]))
        detail = self._event_details(queue["queue_id"], "queue_launched")[0]
        self.assertIn("config.yaml", detail["fingerprint_changed"])

    def test_real_multiprocess_launch_races_repeat_twenty_times_each(self):
        choose = run_race_scenario(self.root / "race-choose", "choose_choose", iterations=20)
        tick = run_race_scenario(self.root / "race-tick", "tick_choose", iterations=20)
        self.assertTrue(choose["ok"], choose)
        self.assertTrue(tick["ok"], tick)


if __name__ == "__main__":
    unittest.main()
