import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from labops import gpuq, mcp_worker
from labops import cli
from labops.cli import build_parser
from labops.db import connect, migrate
from labops.errors import IllegalTransition, ValidationError
from labops.gpuq import (gpu_busy, queue_cancel, queue_choose, queue_intent, queue_list,
                         queue_progress, queue_register, queue_tick)
from labops.notify import _event_notification, _prepare_send_text, notify_config
from labops.progress import progress_summary
from labops.redact import for_actor
from labops.tasks import set_repo_options


class GpuQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-gpuq-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "registered-repo"
        self.repo.mkdir()
        scripts = self.repo / "scripts"
        scripts.mkdir()
        self.entrypoint = scripts / "train.ps1"
        self.entrypoint.write_text("Write-Output 'toy'\n", encoding="utf-8")
        self.conn = connect(self.root / "home" / "labops.db")
        migrate(self.conn)
        self._add_repo("toy", self.repo)

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

    def _register(self, title="GPU task", **kwargs):
        with patch.object(gpuq, "_ALLOWED_ENTRY_ROOTS", (self.root,)):
            return queue_register(
                self.conn, repo_id=kwargs.pop("repo_id", "toy"), title=title,
                entrypoint=kwargs.pop("entrypoint", str(self.entrypoint)),
                context=kwargs.pop("context", "synthetic unit test"), **kwargs,
            )["queue_item"]

    def _add_run(self, run_key, *, repo_id="toy", script_path=None, status="running", pid=1234,
                 started_at=None, finished_at=None, exit_code=None):
        self.conn.execute(
            "INSERT INTO bg_runs(run_key,repo_id,run_dir,script_path,status,pid,started_at,"
            "finished_at,exit_code,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_key, repo_id, str(self.root / "runs" / run_key[:12]),
             str(script_path or self.repo / "other.ps1"), status, pid,
             started_at or self._ts(-5), finished_at, exit_code, self._ts()),
        )

    def _event_details(self, queue_id, notification_kind=None):
        rows = self.conn.execute(
            "SELECT detail_json FROM events WHERE entity_type='gpu_queue' AND entity_id=? ORDER BY seq",
            (queue_id,),
        ).fetchall()
        details = [json.loads(row[0]) for row in rows]
        if notification_kind is not None:
            return [item for item in details if item.get("notification_kind") == notification_kind]
        return details

    def _set_launch_mode(self, mode):
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES('queue_launch_mode',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (mode,),
        )

    def _ask_idle(self, queue_id):
        queue_tick(self.conn, self._ts(), pid_alive=lambda _pid: False)
        return queue_choose(self.conn, queue_id=queue_id, choice="auto", actor="secretary",
                            pid_alive=lambda _pid: False, now_ts=self._ts())

    def test_local_transition_rejects_illegal_edge_and_writes_canonical_event(self):
        draft = queue_intent(self.conn, repo_id="toy", title="intent", note="ask", created_by="secretary")["queue_item"]
        with self.assertRaises(IllegalTransition):
            gpuq._transition(self.conn, draft["queue_id"], "LAUNCHING", actor="codex")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE entity_type='gpu_queue' AND entity_id=?",
            (draft["queue_id"],),
        ).fetchone()[0], 1)

        queued = self._register("registered", draft_id=draft["queue_id"], created_by="opus")
        event = self.conn.execute(
            "SELECT * FROM events WHERE entity_type='gpu_queue' AND entity_id=? AND to_state='QUEUED'",
            (queued["queue_id"],),
        ).fetchone()
        self.assertEqual((event["from_state"], event["to_state"], event["actor"]),
                         ("DRAFT", "QUEUED", "opus"))
        detail = json.loads(event["detail_json"])
        self.assertEqual(detail["entrypoint"], str(self.entrypoint.resolve()))
        self.assertEqual(detail["entrypoint_sha256"], queued["entrypoint_sha256"])
        self.assertTrue(event["ts"])

    def test_busy_queue_asks_once_and_manual_idle_once_includes_failed_previous(self):
        queue = self._register("manual after GPU")
        queue_id = queue["queue_id"]
        self._add_run("a" * 64, status="running", pid=222)
        alive = lambda pid: pid == 222

        first = queue_tick(self.conn, self._ts(), pid_alive=alive)
        second = queue_tick(self.conn, self._ts(1), pid_alive=alive)
        self.assertEqual(first["asked"], 1)
        self.assertEqual(second["asked"], 0)
        self.assertEqual(len(self._event_details(queue_id, "queue_ask_busy")), 1)
        queue_choose(self.conn, queue_id=queue_id, choice="manual", actor="secretary", now_ts=self._ts(2))

        self.conn.execute("UPDATE bg_runs SET status='failed',finished_at=?,exit_code=7 WHERE run_key=?",
                          (self._ts(3), "a" * 64))
        idle_first = queue_tick(self.conn, self._ts(4), pid_alive=alive)
        idle_again = queue_tick(self.conn, self._ts(5), pid_alive=alive)
        self.assertEqual(idle_first["manual_idle_notified"], 1)
        self.assertEqual(idle_again["manual_idle_notified"], 0)
        notices = self._event_details(queue_id, "queue_idle_manual")
        self.assertEqual(len(notices), 1)
        self.assertIn("失败 exit 7", notices[0]["previous_run"]["result"])
        self.assertIn("-ScriptPath", notices[0]["manual_command"])

    def test_auto_handoff_stays_launching_and_only_hands_off_once(self):
        queue = self._register("auto handoff")
        queue_id = queue["queue_id"]
        self._ask_idle(queue_id)
        first = queue_tick(self.conn, self._ts(2), pid_alive=lambda _pid: False)
        second = queue_tick(self.conn, self._ts(3), pid_alive=lambda _pid: False)
        row = self.conn.execute("SELECT status,launch_attempted_at FROM gpu_queue WHERE queue_id=?",
                                (queue_id,)).fetchone()
        self.assertEqual(row["status"], "LAUNCHING")
        self.assertTrue(row["launch_attempted_at"])
        self.assertEqual(first["handed_off"], 1)
        self.assertEqual(second["handed_off"], 0)
        notices = self._event_details(queue_id, "queue_idle_manual")
        self.assertEqual(len(notices), 1)
        self.assertTrue(notices[0]["handoff"])
        self.assertIn(str(self.entrypoint), notices[0]["manual_command"])
        self.assertIn("auto-handoff", notices[0]["manual_command"])
        event = self.conn.execute(
            "SELECT * FROM events WHERE entity_type='gpu_queue' AND entity_id=? ORDER BY seq DESC LIMIT 1",
            (queue_id,),
        ).fetchone()
        notification = _event_notification(self.conn, event)
        lines = notification["text"].splitlines()
        self.assertEqual(lines[0], f"[LabOps · toy] auto handoff（{queue_id}）")
        self.assertEqual(lines[1], "GPU 已空闲 / 按你的选择应启动")
        self.assertIn(f"background_start 启动 {self.entrypoint}，run 目录前缀 auto-handoff", lines[2])
        self.assertEqual(lines[3], "任务：auto handoff")

        notify_config(self.conn, llm_rewrite="on")
        proposals = {
            "queue-handoff-path": notification["text"].replace(str(self.entrypoint), "removed"),
            "queue-handoff-id": notification["text"].replace(queue_id, "replaced-id"),
        }
        for event_key, proposal in proposals.items():
            with self.subTest(event_key=event_key):
                self.conn.execute(
                    "INSERT INTO notifications(event_key,kind,text,status,created_at) VALUES(?,?,?,?,?)",
                    (event_key, "queue_idle_manual", notification["text"], "PENDING", self._ts()),
                )
                with patch("labops.rewrite.rewrite", return_value=(proposal, "llm")):
                    sent = _prepare_send_text(self.conn, event_key, notification["text"])
                saved = self.conn.execute(
                    "SELECT rewrite_outcome FROM notifications WHERE event_key=?", (event_key,)
                ).fetchone()[0]
                self.assertEqual((sent, saved), (notification["text"], "template_check_failed"))

    def test_launcher_mode_is_mocked_and_failed_predecessor_does_not_block(self):
        self._set_launch_mode("launcher")
        self._add_run("b" * 64, status="failed", pid=0, exit_code=9,
                      finished_at=self._ts(-1))
        queue = self._register("after failure")
        queue_id = queue["queue_id"]
        self._ask_idle(queue_id)
        calls = []

        def fake_launcher(row, run_dir):
            calls.append((row["queue_id"], run_dir))
            return {"ok": True}

        result = queue_tick(self.conn, self._ts(2), pid_alive=lambda _pid: False,
                            launcher=fake_launcher)
        self.assertEqual(result["launching"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], queue_id)
        self.assertIn("after-failure_", calls[0][1])
        self.assertEqual(self.conn.execute("SELECT status FROM gpu_queue WHERE queue_id=?",
                                           (queue_id,)).fetchone()[0], "LAUNCHING")

        run_key = "c" * 64
        self._add_run(run_key, script_path=self.entrypoint, status="completed", pid=0,
                      started_at=self._ts(3), finished_at=self._ts(4), exit_code=0)
        matched = queue_tick(self.conn, self._ts(5), pid_alive=lambda _pid: False,
                             launcher=fake_launcher)
        final = self.conn.execute("SELECT status,launched_run_key FROM gpu_queue WHERE queue_id=?",
                                  (queue_id,)).fetchone()
        self.assertEqual(matched["matched"], 1)
        self.assertEqual((final["status"], final["launched_run_key"]), ("LAUNCHED", run_key))
        self.assertEqual(len(self._event_details(queue_id, "queue_launched")), 1)

    def test_launcher_hash_guard_fails_before_invocation(self):
        self._set_launch_mode("launcher")
        queue = self._register("hash guard")
        queue_id = queue["queue_id"]
        queue_tick(self.conn, self._ts(), pid_alive=lambda _pid: False)
        queue_choose(self.conn, queue_id=queue_id, choice="now", actor="secretary",
                     pid_alive=lambda _pid: False, now_ts=self._ts(1))
        self.entrypoint.write_text("Write-Output 'changed'\n", encoding="utf-8")
        calls = []
        result = queue_tick(self.conn, self._ts(2), pid_alive=lambda _pid: False,
                            launcher=lambda *_args: calls.append(True) or {"ok": True})
        row = self.conn.execute("SELECT status,launch_error FROM gpu_queue WHERE queue_id=?",
                                (queue_id,)).fetchone()
        self.assertEqual(result["launch_failed"], 1)
        self.assertEqual((row["status"], row["launch_error"]), ("LAUNCH_FAILED", "entrypoint_changed"))
        self.assertEqual(calls, [])
        self.assertEqual(self._event_details(queue_id, "queue_launch_failed")[0]["reason"],
                         "entrypoint_changed")

    def test_matching_manual_run_dequeues_queue_item(self):
        queue = self._register("manual run")
        run_key = "d" * 64
        self._add_run(run_key, script_path=self.entrypoint, status="running", pid=333,
                      started_at=self._ts(1))
        result = queue_tick(self.conn, self._ts(2), pid_alive=lambda pid: pid == 333)
        row = self.conn.execute("SELECT status,launched_run_key FROM gpu_queue WHERE queue_id=?",
                                (queue["queue_id"],)).fetchone()
        self.assertEqual(result["matched"], 1)
        self.assertEqual((row["status"], row["launched_run_key"]), ("LAUNCHED", run_key))
        self.assertEqual(result["asked"], 0)

    def test_fifo_launches_at_most_one_item_per_tick(self):
        self._set_launch_mode("launcher")
        first = self._register("first")
        second = self._register("second")
        self.conn.execute("UPDATE gpu_queue SET created_at=? WHERE queue_id=?", (self._ts(-3), first["queue_id"]))
        self.conn.execute("UPDATE gpu_queue SET created_at=? WHERE queue_id=?", (self._ts(-2), second["queue_id"]))
        queue_tick(self.conn, self._ts(), pid_alive=lambda _pid: False)
        for item in (first, second):
            queue_choose(self.conn, queue_id=item["queue_id"], choice="auto", actor="secretary",
                         pid_alive=lambda _pid: False, now_ts=self._ts(1))
        calls = []

        def fake_launcher(row, _run_dir):
            calls.append(row["queue_id"])
            return {"ok": True}

        first_tick = queue_tick(self.conn, self._ts(2), pid_alive=lambda _pid: False,
                                launcher=fake_launcher)
        rows = {row["queue_id"]: row["status"] for row in self.conn.execute(
            "SELECT queue_id,status FROM gpu_queue WHERE queue_id IN (?,?)",
            (first["queue_id"], second["queue_id"]),
        )}
        self.assertEqual(first_tick["launching"], 1)
        self.assertEqual(calls, [first["queue_id"]])
        self.assertEqual(rows, {first["queue_id"]: "LAUNCHING", second["queue_id"]: "WAIT_AUTO"})

        self._add_run("e" * 64, script_path=self.entrypoint, status="completed", pid=0,
                      started_at=self._ts(3), finished_at=self._ts(4), exit_code=0)
        second_tick = queue_tick(self.conn, self._ts(5), pid_alive=lambda _pid: False,
                                 launcher=fake_launcher)
        self.assertEqual(second_tick["matched"], 1)
        self.assertEqual(second_tick["launching"], 1)
        self.assertEqual(calls, [first["queue_id"], second["queue_id"]])
        active_launches = self.conn.execute("SELECT COUNT(*) FROM gpu_queue WHERE status='LAUNCHING'").fetchone()[0]
        self.assertEqual(active_launches, 1)

    def test_now_choice_refuses_a_second_concurrent_launch(self):
        first = self._register("first launch")
        second = self._register("second launch")
        queue_tick(self.conn, self._ts(), pid_alive=lambda _pid: False)
        queue_choose(self.conn, queue_id=first["queue_id"], choice="now", actor="secretary",
                     pid_alive=lambda _pid: False, now_ts=self._ts(1))
        with self.assertRaises(ValidationError) as raised:
            queue_choose(self.conn, queue_id=second["queue_id"], choice="now", actor="secretary",
                         pid_alive=lambda _pid: False, now_ts=self._ts(2))
        self.assertEqual(str(raised.exception), "queue_launch_in_progress")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM gpu_queue WHERE status='LAUNCHING'").fetchone()[0], 1)

    def test_cancel_and_secretary_register_permissions(self):
        queue = self._register("cancel me")
        queue_id = queue["queue_id"]
        queue_tick(self.conn, self._ts(), pid_alive=lambda _pid: False)
        cancelled = queue_cancel(self.conn, queue_id=queue_id, reason="user requested", actor="codex")
        self.assertEqual(cancelled["queue_item"]["status"], "CANCELLED")

        self.assertNotIn("queue_register", mcp_worker._ACTOR_TOOLS["secretary"])
        with self.assertRaises(ValidationError) as raised:
            mcp_worker._dispatch("queue_register", {
                "repo_id": "toy", "title": "forbidden", "entrypoint": str(self.entrypoint),
                "context": "should never be accepted",
            }, "secretary")
        self.assertEqual(str(raised.exception), "actor_tool_not_allowed")
        with patch.object(gpuq, "_ALLOWED_ENTRY_ROOTS", (self.root,)):
            with self.assertRaises(ValidationError) as raised:
                queue_register(self.conn, repo_id="toy", title="forbidden",
                               entrypoint=str(self.entrypoint), context="forbidden",
                               created_by="secretary")
        self.assertEqual(str(raised.exception), "queue_register_actor_not_allowed")

    def test_confidential_queue_and_gpu_details_are_redacted_for_secretary(self):
        private_repo = self.root / "private-repo"
        private_repo.mkdir()
        self._add_repo("private", private_repo, confidential=True)
        item = queue_intent(self.conn, repo_id="private", title="private title", note="private note",
                            created_by="secretary")["queue_item"]
        self._add_run("f" * 64, repo_id="private", status="running", pid=345)

        raw = queue_list(self.conn, pid_alive=lambda _pid: True)
        safe = for_actor(self.conn, "secretary", raw)

        private_item = next(row for row in safe["queue"] if row.get("title") == "private title")
        self.assertEqual(private_item, {"title": "private title", "status": "DRAFT"})
        self.assertEqual(safe["gpu"]["busy"], True)
        self.assertEqual(safe["gpu"]["runs"], [])
        self.assertNotIn("private note", json.dumps(safe, ensure_ascii=False))

        summary = progress_summary(self.conn)
        self.assertIn("queue", summary)
        self.assertEqual(summary["queue"]["count"], 1)
        progress_safe = for_actor(self.conn, "secretary", summary)
        private_progress = next(row for row in progress_safe["queue"]["items"] if row.get("title") == "private title")
        self.assertEqual(private_progress, {"title": "private title", "status": "DRAFT"})

    def test_watch_glob_options_are_normalized_validated_and_exposed_by_cli(self):
        result = set_repo_options(self.conn, "toy", run_watch=[r"data\thin_slice\**\*.jsonl", "runs/*.jsonl"])
        self.assertEqual(result["run_watch_globs"], ["data/thin_slice/**/*.jsonl", "runs/*.jsonl"])
        saved = self.conn.execute("SELECT run_watch_globs FROM repos WHERE repo_id='toy'").fetchone()[0]
        self.assertEqual(json.loads(saved), result["run_watch_globs"])
        with self.assertRaises(ValidationError):
            set_repo_options(self.conn, "toy", run_watch=["../outside/*.jsonl"])
        cleared = set_repo_options(self.conn, "toy", run_watch=[""])
        self.assertEqual(cleared["run_watch_globs"], [])
        args = build_parser().parse_args([
            "repo", "set", "--repo-id", "toy", "--run-watch", "one/*.jsonl",
            "--run-watch", "two/*.jsonl",
        ])
        self.assertEqual(args.run_watch, ["one/*.jsonl", "two/*.jsonl"])
        queue_args = build_parser().parse_args(["queue", "list"])
        with patch.object(cli, "_db", return_value=self.conn):
            listed = cli.dispatch(queue_args)
        self.assertEqual(listed["queue"], [])

    def test_orphaned_process_is_not_gpu_busy_and_event_is_reportable_once(self):
        run_key = "1" * 64
        self._add_run(run_key, status="running", pid=9876)
        first = gpu_busy(self.conn, now_ts=self._ts(), pid_alive=lambda _pid: False)
        second = gpu_busy(self.conn, now_ts=self._ts(1), pid_alive=lambda _pid: False)
        self.assertEqual(first, {"busy": False, "runs": []})
        self.assertEqual(second["busy"], False)
        self.assertEqual(self.conn.execute("SELECT orphaned FROM bg_runs WHERE run_key=?", (run_key,)).fetchone()[0], 1)
        events = self.conn.execute(
            "SELECT * FROM events WHERE entity_type='bg_run' AND entity_id=? AND to_state='run_orphaned'",
            (run_key,),
        ).fetchall()
        self.assertEqual(len(events), 1)
        notification = _event_notification(self.conn, events[0])
        self.assertEqual(notification["kind"], "run_orphaned")


if __name__ == "__main__":
    unittest.main()
