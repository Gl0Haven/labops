import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from labops import cli
from labops.cli import build_parser
from labops.db import connect, migrate
from labops.notify import (_event_notification, _urgent_kinds, merge_pending, notify_config,
                           run as notify_run, scan, send_pending)
from labops.progress import progress_summary
from labops.redact import for_actor
from labops.rewrite import _request_payload, rewrite
from labops.runs import list_runs, scan_runs


class RunsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-runs-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "registered-repo"
        self.repo.mkdir()
        self.state = self.root / "background-state"
        self.state.mkdir()
        self.conn = connect(self.root / "home" / "labops.db")
        migrate(self.conn)
        self._add_repo("toy", self.repo)
        self.env = patch.dict(os.environ, {"LABOPS_BG_STATE_ROOT": str(self.state), "LABOPS_NTFY_TOPIC": ""})
        self.env.start()

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def _add_repo(self, repo_id: str, path: Path, confidential: bool = False):
        self.conn.execute(
            "INSERT INTO repos(repo_id,path,confidential,created_at) VALUES(?,?,?,?)",
            (repo_id, str(path), int(confidential), "2026-10-20T10:00:00Z"),
        )

    def _write_run(self, key: str, status: str, *, working_directory: Path | None = None,
                   output: str | None = None, error: str | None = None, exit_code: int | None = None):
        directory = self.state / key
        directory.mkdir(parents=True, exist_ok=True)
        run_dir = self.root / "runs" / f"run-{key[:6]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        if output is not None:
            (run_dir / "output.log").write_bytes(output.encode("utf-8"))
        if error is not None:
            (run_dir / "error.log").write_bytes(error.encode("utf-8"))
        (directory / "launch.json").write_text(json.dumps({
            "schemaVersion": 4,
            "runDirectory": str(run_dir),
            "scriptPath": str(self.repo / "train.py"),
            "workingDirectory": str(working_directory or self.repo),
            "requestedAtUtc": "2026-10-20T11:00:00Z",
        }), encoding="utf-8")
        (directory / "process.json").write_text(json.dumps({
            "pid": 1234,
            "processStartedAtUtc": "2026-10-20T11:00:00Z",
            "runDirectory": str(run_dir),
            "logPath": str(run_dir / "output.log"),
        }), encoding="utf-8")
        (directory / "status.json").write_text(json.dumps({
            "status": status,
            "pid": 1234,
            "startedAtUtc": "2026-10-20T11:00:00Z",
            "finishedAtUtc": "2026-10-20T11:30:00Z" if status in {"completed", "failed", "stopped"} else None,
            "exitCode": exit_code,
        }), encoding="utf-8")
        return directory, run_dir

    def _set_status(self, directory: Path, status: str, exit_code: int | None = None):
        path = directory / "status.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["status"] = status
        value["finishedAtUtc"] = "2026-10-20T12:01:00Z" if status != "running" else None
        value["exitCode"] = exit_code
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_initial_history_is_silent_bad_json_skips_and_unknown_repo_is_ignored(self):
        self._write_run("a" * 64, "completed")
        self._write_run("b" * 64, "completed", working_directory=self.root / "registered-repo-other")
        broken = self.state / ("c" * 64)
        broken.mkdir()
        (broken / "launch.json").write_text("{", encoding="utf-8")
        result = scan_runs(self.conn, now_ts="2026-10-20T12:00:00Z")
        self.assertEqual(result, {"seen": 2, "tracked": 1, "events": 0, "skipped": 1,
                                  "watch_truncated": 0, "progress_rule_errors": 0})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bg_runs").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE entity_type='bg_run'").fetchone()[0], 0)

    def test_status_transitions_tqdm_carriage_returns_and_ansi_stripping(self):
        directory, run_dir = self._write_run(
            "d" * 64, "running", working_directory=str(self.repo).upper().replace("\\", "/"),
            output="epoch 1\rstep 2\r\x1b[32m5/9\x1b[0m\r",
        )
        started = scan_runs(self.conn, now_ts="2026-10-20T12:00:00Z")
        self.assertEqual(started["events"], 1)
        row = self.conn.execute("SELECT * FROM bg_runs WHERE run_key=?", ("d" * 64,)).fetchone()
        self.assertEqual(row["source"], "background_run")
        self.assertEqual(row["progress_hint"], "5/9")
        self._set_status(directory, "completed", 0)
        self.assertEqual(scan_runs(self.conn, now_ts="2026-10-20T12:01:00Z")["events"], 1)
        self.assertEqual([row["to_state"] for row in self.conn.execute(
            "SELECT to_state FROM events WHERE entity_type='bg_run' ORDER BY seq")],
            ["run_started", "run_completed"])

        failed_dir, _ = self._write_run("e" * 64, "running", output="iter 7\n", error="Traceback\rboom\n")
        scan_runs(self.conn, now_ts="2026-10-20T12:02:00Z")
        self._set_status(failed_dir, "failed", 2)
        scan_runs(self.conn, now_ts="2026-10-20T12:03:00Z")
        failed_event = self.conn.execute(
            "SELECT * FROM events WHERE entity_id=? AND to_state='run_failed'", ("e" * 64,)
        ).fetchone()
        notification = _event_notification(self.conn, failed_event)
        self.assertEqual(notification["kind"], "run_failed")
        self.assertIn("exit code 2 · boom", notification["text"])

    def test_stall_alert_is_once_until_log_changes(self):
        directory, run_dir = self._write_run("f" * 64, "running", output="step 10\n")
        self.conn.execute("INSERT INTO meta(key,value) VALUES('runs_stall_minutes','1')")
        log_path = run_dir / "output.log"
        fresh_mtime = datetime.fromisoformat("2026-10-20T11:59:00+00:00").timestamp()
        os.utime(log_path, (fresh_mtime, fresh_mtime))
        self.assertEqual(scan_runs(self.conn, now_ts="2026-10-20T12:00:00Z")["events"], 1)
        old_mtime = datetime.fromisoformat("2026-10-20T11:58:00+00:00").timestamp()
        os.utime(log_path, (old_mtime, old_mtime))
        first = scan_runs(self.conn, now_ts="2026-10-20T12:00:01Z")
        self.assertEqual(first["events"], 1)  # stall after a fresh run was observed
        self.assertEqual(scan_runs(self.conn, now_ts="2026-10-20T12:00:30Z")["events"], 0)

        log_path.write_text("step 11\n", encoding="utf-8")
        refreshed = datetime.fromisoformat("2026-10-20T12:01:00+00:00").timestamp()
        os.utime(log_path, (refreshed, refreshed))
        self.assertEqual(scan_runs(self.conn, now_ts="2026-10-20T12:01:30Z")["events"], 0)
        self.assertEqual(self.conn.execute("SELECT stall_notified FROM bg_runs WHERE run_key=?",
                                           ("f" * 64,)).fetchone()[0], 0)
        second_stall = scan_runs(self.conn, now_ts="2026-10-20T12:03:01Z")
        self.assertEqual(second_stall["events"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE entity_id=? AND to_state='run_stalled'",
                                           ("f" * 64,)).fetchone()[0], 2)

    def test_watch_glob_mtime_extends_run_progress_time(self):
        self.conn.execute("INSERT INTO meta(key,value) VALUES('runs_stall_minutes','1')")
        self.conn.execute("UPDATE repos SET run_watch_globs=? WHERE repo_id='toy'",
                          (json.dumps(["data/thin_slice/**/*.jsonl"]),))
        _directory, run_dir = self._write_run("9" * 64, "running", output="step 2/10\n")
        log_path = run_dir / "output.log"
        watched = self.repo / "data" / "thin_slice" / "metrics.jsonl"
        watched.parent.mkdir(parents=True)
        watched.write_text('{"step":3}\n', encoding="utf-8")
        log_mtime = datetime.fromisoformat("2026-10-20T11:40:00+00:00").timestamp()
        watched_mtime = datetime.fromisoformat("2026-10-20T11:59:30+00:00").timestamp()
        os.utime(log_path, (log_mtime, log_mtime))
        os.utime(watched, (watched_mtime, watched_mtime))

        result = scan_runs(self.conn, now_ts="2026-10-20T12:00:00Z")
        still_fresh = scan_runs(self.conn, now_ts="2026-10-20T12:00:20Z")
        stalled = scan_runs(self.conn, now_ts="2026-10-20T12:01:00Z")
        repeated = scan_runs(self.conn, now_ts="2026-10-20T12:01:20Z")

        row = self.conn.execute("SELECT log_mtime,progress_at FROM bg_runs WHERE run_key=?",
                                ("9" * 64,)).fetchone()
        self.assertEqual(result["watch_truncated"], 0)
        self.assertEqual(row["log_mtime"], "2026-10-20T11:59:30Z")
        self.assertEqual(row["progress_at"], "2026-10-20T12:00:00Z")
        self.assertEqual(still_fresh["events"], 0)
        self.assertEqual(stalled["events"], 1)
        self.assertEqual(repeated["events"], 0)
        self.assertEqual([row[0] for row in self.conn.execute(
            "SELECT to_state FROM events WHERE entity_id=? ORDER BY seq", ("9" * 64,),
        )], ["run_started", "run_stalled"])

    def test_first_seen_stale_or_logless_runs_are_silent_until_logs_update(self):
        stale_dir, stale_run = self._write_run("7" * 64, "running", output="step 1\n")
        missing_dir, missing_run = self._write_run("8" * 64, "running")
        self.conn.execute("INSERT INTO meta(key,value) VALUES('runs_stall_minutes','1')")
        old_mtime = datetime.fromisoformat("2026-10-20T11:58:00+00:00").timestamp()
        os.utime(stale_run / "output.log", (old_mtime, old_mtime))

        first = scan_runs(self.conn, now_ts="2026-10-20T12:00:01Z")
        self.assertEqual(first["events"], 0)
        self.assertEqual(scan_runs(self.conn, now_ts="2026-10-20T12:00:30Z")["events"], 0)
        states = dict(self.conn.execute(
            "SELECT run_key,stall_notified FROM bg_runs WHERE run_key IN (?,?)",
            ("7" * 64, "8" * 64),
        ).fetchall())
        self.assertEqual(states, {"7" * 64: 1, "8" * 64: 1})

        refreshed = datetime.fromisoformat("2026-10-20T12:01:00+00:00").timestamp()
        stale_log = stale_run / "output.log"
        stale_log.write_text("step 2\n", encoding="utf-8")
        os.utime(stale_log, (refreshed, refreshed))
        missing_log = missing_run / "output.log"
        missing_log.write_text("epoch 1/4\n", encoding="utf-8")
        os.utime(missing_log, (refreshed, refreshed))
        resumed = scan_runs(self.conn, now_ts="2026-10-20T12:01:10Z")
        self.assertEqual(resumed["events"], 0)
        rows = {row["run_key"]: row for row in self.conn.execute(
            "SELECT run_key,stall_notified,progress_hint FROM bg_runs WHERE run_key IN (?,?)",
            ("7" * 64, "8" * 64),
        )}
        self.assertEqual(rows["7" * 64]["progress_hint"], "step 2")
        self.assertEqual(rows["8" * 64]["progress_hint"], "epoch 1/4")
        self.assertEqual([row["stall_notified"] for row in rows.values()], [0, 0])
        aged_again = scan_runs(self.conn, now_ts="2026-10-20T12:02:01Z")
        self.assertEqual(aged_again["events"], 2)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE to_state='run_stalled' AND entity_id IN (?,?)",
            ("7" * 64, "8" * 64),
        ).fetchone()[0], 2)

    def test_digest_progress_only_snapshot_changes_once_and_cli_lists_runs(self):
        notify_config(self.conn, digest_minutes=0)
        self.conn.execute(
            "INSERT INTO bg_runs(run_key,repo_id,run_dir,status,progress_hint,progress_at,log_mtime,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("1" * 64, "toy", str(self.root / "runs" / "trial-a"), "running", "epoch 2/8",
             "2026-10-20T11:59:00Z", "2026-10-20T11:59:00Z", "2026-10-20T12:00:00Z"),
        )
        initial = merge_pending(self.conn, now_ts="2026-10-20T12:00:00Z")
        self.assertEqual(initial["merged_digest"], 0)
        batch = self.conn.execute("SELECT * FROM notifications WHERE kind='batch_digest'").fetchone()
        self.assertIn("训练进度", batch["text"])
        self.assertIn("trial-a · epoch 2/8", batch["text"])
        self.assertIn("进度快照", batch["text"])
        send_pending(self.conn, lambda _text: "sent")
        merge_pending(self.conn, now_ts="2026-10-20T12:01:00Z")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications WHERE kind='batch_digest'").fetchone()[0], 1)

        self.conn.execute("UPDATE bg_runs SET progress_hint='epoch 3/8',log_mtime='2026-10-20T12:01:00Z' WHERE run_key=?",
                          ("1" * 64,))
        merge_pending(self.conn, now_ts="2026-10-20T12:01:01Z")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications WHERE kind='batch_digest'").fetchone()[0], 2)
        latest = self.conn.execute("SELECT text FROM notifications WHERE kind='batch_digest' ORDER BY created_at DESC,event_key DESC LIMIT 1").fetchone()[0]
        self.assertIn("epoch 3/8", latest)
        self.assertEqual(list_runs(self.conn, "toy")[0]["run_key"], "1" * 64)
        args = build_parser().parse_args(["runs", "list", "--repo-id", "toy"])
        self.assertEqual((args.command, args.runs_command, args.repo_id), ("runs", "list", "toy"))

    def test_notify_run_scans_background_state_before_event_outbox(self):
        notify_config(self.conn, digest_minutes=0)
        self._write_run("6" * 64, "running", output="epoch 4/12\n")
        sent = []
        with patch("labops.gpuq._pid_alive", return_value=True):
            result = notify_run(self.conn, sender=lambda text: sent.append(text) or "sent")
        self.assertEqual(result["runs"], {"seen": 1, "tracked": 1, "events": 1, "skipped": 0,
                                           "watch_truncated": 0, "progress_rule_errors": 0})
        self.assertEqual(result["accepted"], 1)
        self.assertIn("run-666666 · epoch 4/12", sent[0])
        self.assertIn("训练进度", sent[0])

    def test_failed_and_stalled_are_urgent_and_secretary_redacts_private_runs(self):
        self.assertTrue({"run_failed", "run_stalled"}.issubset(_urgent_kinds(self.conn)))
        directory, _run_dir = self._write_run("2" * 64, "running", error="boom\n")
        scan_runs(self.conn, now_ts="2026-10-20T12:00:00Z")
        self._set_status(directory, "failed", 3)
        scan_runs(self.conn, now_ts="2026-10-20T12:01:00Z")
        scan(self.conn)
        sent = []
        send_pending(self.conn, lambda text: sent.append(text) or "sent")
        self.assertEqual(len(sent), 1)
        self.assertIn("训练 run-222222 · 失败", sent[0])

        self._add_repo("private", self.root / "private-repo", confidential=True)
        self.conn.execute(
            "INSERT INTO bg_runs(run_key,repo_id,run_dir,status,pid,started_at,progress_hint,progress_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("3" * 64, "private", "C:/private/runs/secret-run", "running", 99,
             "2026-10-20T11:00:00Z", "secret metric 8/10", "2026-10-20T12:00:00Z", "2026-10-20T12:00:00Z"),
        )
        with patch("labops.progress.now", return_value="2026-10-20T12:05:00Z"):
            summary = progress_summary(self.conn)
        secretary = for_actor(self.conn, "secretary", summary)
        private = next(item for item in secretary["runs"] if item.get("status") == "running"
                       and "run_dir" not in item)
        self.assertEqual(set(private), {"status", "started_at", "finished_at"})
        self.assertNotIn("secret-run", json.dumps(secretary, ensure_ascii=False))
        self.assertTrue(any(item.get("repo_id") == "toy" and "run_dir" in item for item in secretary["runs"]))

    def test_llm_rewrite_guards_validation_and_retry_reuses_saved_text(self):
        template = (
            "[LabOps · toy] 训练 trial-a · 完成 T20261020-labops-runs r1 · 用时 12 分钟\n"
            "下一步：转给 Codex：调用 next_task\n任务：训练进度接入 DeepSeek"
        )
        self.assertEqual(rewrite(template, kind="batch_digest", confidential=False, enabled=False),
                         (template, "template_disabled"))
        self.assertEqual(rewrite(template, kind="batch_digest", confidential=True, enabled=True,
                                 api_key_available=True), (template, "template_confidential"))
        self.assertEqual(rewrite(template, kind="batch_digest", confidential=False, enabled=True,
                                 api_key_available=False), (template, "template_disabled"))

        with patch("labops.rewrite._request_completion", return_value=template) as request:
            rewritten = rewrite(template, kind="batch_digest", confidential=False, enabled=True,
                                api_key_available=True)
        self.assertEqual(rewritten, (template, "llm"))
        request.assert_called_once()
        with patch("labops.rewrite._request_completion", return_value=template.replace("T20261020-labops-runs", "removed")):
            self.assertEqual(rewrite(template, kind="batch_digest", confidential=False, enabled=True,
                                     api_key_available=True), (template, "template_check_failed"))
        with patch("labops.rewrite._request_completion", return_value=template.replace("\n任务：训练进度接入 DeepSeek", "")):
            self.assertEqual(rewrite(template, kind="batch_digest", confidential=False, enabled=True,
                                     api_key_available=True), (template, "template_check_failed"))
        with patch("labops.rewrite._request_completion", side_effect=TimeoutError):
            self.assertEqual(rewrite(template, kind="run_failed", confidential=False, enabled=True,
                                     api_key_available=True), (template, "template_error"))

        notify_config(self.conn, llm_rewrite="on")
        self.conn.execute(
            "INSERT INTO notifications(event_key,kind,text,status,created_at) VALUES(?,?,?,?,?)",
            ("ev:rewrite", "run_failed", template, "PENDING", "2026-10-20T12:00:00Z"),
        )
        rewrite_calls = []
        ntfy_calls = []

        def fake_rewrite(text, **_kwargs):
            rewrite_calls.append(text)
            return "已润色：" + text, "llm"

        with patch("labops.rewrite.rewrite", side_effect=fake_rewrite):
            first = send_pending(self.conn, lambda _text: "failed", ntfy_sender=lambda text: ntfy_calls.append(text) or "sent",
                                 ntfy_topic="mock-topic")
            second = send_pending(self.conn, lambda _text: "sent", ntfy_sender=lambda text: ntfy_calls.append(text) or "sent",
                                  ntfy_topic="mock-topic")
        self.assertEqual((first["failed"], second["accepted"]), (1, 1))
        self.assertEqual(len(rewrite_calls), 1)
        self.assertEqual(ntfy_calls, [template.splitlines()[0]])
        saved = self.conn.execute("SELECT rewritten_text,rewrite_outcome FROM notifications WHERE event_key='ev:rewrite'").fetchone()
        self.assertEqual(tuple(saved), ("已润色：" + template, "llm"))

    def test_deepseek_request_body_uses_api_model_without_provider_prefix(self):
        with patch.dict(os.environ, {"LABOPS_DS_MODEL": ""}):
            default_body = json.loads(json.dumps(_request_payload("template"), ensure_ascii=False))
        self.assertEqual(default_body["model"], "deepseek-flash")
        with patch.dict(os.environ, {"LABOPS_DS_MODEL": "deepseek/deepseek-flash"}):
            prefixed_body = json.loads(json.dumps(_request_payload("template"), ensure_ascii=False))
        self.assertEqual(prefixed_body["model"], "deepseek-flash")

    def test_cli_runs_list_and_rewrite_switch_dispatch(self):
        self.conn.execute(
            "INSERT INTO bg_runs(run_key,repo_id,run_dir,status,updated_at) VALUES(?,?,?,?,?)",
            ("4" * 64, "toy", str(self.root / "runs" / "cli-trial"), "running", "2026-10-20T12:00:00Z"),
        )
        cli_db = connect(self.root / "cli-home" / "labops.db")
        migrate(cli_db)
        cli_db.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy',?,?)",
                       (str(self.repo), "2026-10-20T10:00:00Z"))
        cli_db.execute(
            "INSERT INTO bg_runs(run_key,repo_id,run_dir,status,updated_at) VALUES(?,?,?,?,?)",
            ("5" * 64, "toy", str(self.root / "runs" / "cli-trial"), "running", "2026-10-20T12:00:00Z"),
        )
        args = build_parser().parse_args(["runs", "list", "--repo-id", "toy"])
        with patch("labops.cli._db", return_value=cli_db):
            listed = cli.dispatch(args)
        self.assertEqual(len(listed["runs"]), 1)
        self.assertEqual(listed["runs"][0]["run_key"], "5" * 64)

        config_db = connect(self.root / "config-home" / "labops.db")
        migrate(config_db)
        args = build_parser().parse_args(["notify", "config", "--llm-rewrite", "on"])
        with patch("labops.cli._db", return_value=config_db):
            configured = cli.dispatch(args)
        self.assertEqual(configured["llm_rewrite"], "on")


if __name__ == "__main__":
    unittest.main()
