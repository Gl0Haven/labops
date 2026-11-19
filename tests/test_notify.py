import os
import json
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from labops.config import now
from labops.db import connect, migrate
from labops.notify import (NtfySender, OpenClawSender, _event_notification, list_notifications, merge_pending, mute_kind,
                           notify_config, scan, _to_wechat_markdown,
                           send_pending, set_alert_detail, unmute_kind)
from labops.tasks import add_repo, create_task, open_round, set_confidential
from tests.helpers import commit_file, make_repo


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-notify-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.base = make_repo(self.repo)
        self.conn = connect(self.root / "home" / "labops.db")
        migrate(self.conn)
        self.env = patch.dict("os.environ", {"LABOPS_NTFY_TOPIC": ""})
        self.env.start()
        add_repo(self.conn, repo_id="toy", path=self.repo)
        task = create_task(self.conn, repo_id="toy", task_id="T20261020-notify", goal="notify test", profile={"schema": 1})
        self.task_id = task["task_id"]
        self.round = open_round(self.conn, task_id=self.task_id, actor="codex", role="implement")
        self.round_id = self.round["round_id"]
        self.timestamp = now()
        self.profile_hash = self.conn.execute("SELECT profile_hash FROM tasks WHERE task_id=?", (self.task_id,)).fetchone()[0]

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def _event(self, entity_type, entity_id, state, detail=None):
        cursor = self.conn.execute(
            "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
            (now(), entity_type, entity_id, None, state, "test", json.dumps(detail or {})),
        )
        return cursor.lastrowid

    def _pending(self, event_key, text, created_at):
        self.conn.execute(
            "INSERT INTO notifications(event_key,kind,text,status,created_at) VALUES(?, 'verify_failed', ?, 'PENDING', ?)",
            (event_key, text, created_at),
        )

    def _seed_notification_sources(self):
        self._event("round", self.round_id, "REJECTED")
        self._event("round", self.round_id, "BLOCKED", {"reason": "verify_path_exists"})
        self.conn.execute(
            "INSERT INTO reviews(review_id,task_id,round_id,reviewer,state,verdict,independence,base_commit,reviewed_commit,reviewed_tree,scope_id,acceptance_profile_hash,created_at,updated_at) "
            "VALUES('rv-notify',?,?, 'opus','REWORK_REQUIRED','REWORK_REQUIRED','INDEPENDENT',?,?,?,'scope',?,?,?)",
            (self.task_id, self.round_id, self.base, self.base, "tree", self.profile_hash, self.timestamp, self.timestamp),
        )
        self._event("review", "rv-notify", "REWORK_REQUIRED", {"findings": [{"text": "do not include"}]})
        self.conn.execute(
            "INSERT INTO integrations(integration_id,task_id,candidate_commit,candidate_tree,main_before,state,check_json,created_at,updated_at) "
            "VALUES('ig-notify',?,?,?,?,'AWAITING_HUMAN_APPROVAL','{}',?,?)",
            (self.task_id, self.base, "tree", self.base, self.timestamp, self.timestamp),
        )
        self._event("integration", "ig-notify", "AWAITING_HUMAN_APPROVAL")
        self.conn.execute(
            "INSERT INTO operations(op_id,repo_id,kind,status,idempotency_id,on_applied_json,created_at,updated_at) "
            "VALUES('op-notify','toy','test','CONFLICT','notify-op',?,?,?)",
            (json.dumps({"task_id": self.task_id}), self.timestamp, self.timestamp),
        )
        self._event("operation", "op-notify", "CONFLICT")
        self._event("round", self.round_id, "integrity_conflict", {"expected": self.base, "actual": "f" * 40})
        self._event("round", self.round_id, "unattributed_commit", {"tip": "e" * 40})

    def test_scan_covers_five_event_classes_and_is_idempotent(self):
        self._seed_notification_sources()
        self.assertEqual(scan(self.conn), 7)
        self.assertEqual(scan(self.conn), 0)
        rows = list_notifications(self.conn)
        self.assertEqual(len(rows), 7)
        self.assertEqual({row["kind"] for row in rows}, {
            "verify_failed", "verify_blocked", "review_blocking", "awaiting_approval", "integrity",
        })
        self.assertEqual(sum(row["kind"] == "integrity" for row in rows), 3)
        self.assertTrue(all(row["status"] == "PENDING" for row in rows))
        self.assertIn("labops integrate approve --integration ig-notify", next(
            row["text"] for row in rows if row["kind"] == "awaiting_approval"))
        self.assertNotIn("do not include", " ".join(row["text"] for row in rows))
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key='notify_cursor'").fetchone()[0],
                         str(self.conn.execute("SELECT MAX(seq) FROM events").fetchone()[0]))

    def test_failed_retries_but_timeout_becomes_unknown_without_resend(self):
        self._event("round", self.round_id, "REJECTED")
        self._event("round", self.round_id, "BLOCKED", {"reason": "command_unavailable"})
        self._event("round", self.round_id, "integrity_conflict")
        scan(self.conn)
        calls = []

        def first(text):
            calls.append(text)
            return "sent"

        def failed(text):
            calls.append(text)
            return "failed"

        def retry_sent(text):
            calls.append(text)
            return "sent"

        def timeout(text):
            calls.append(text)
            raise TimeoutError("simulated timeout")

        self.assertEqual(send_pending(self.conn, first, limit=1)["accepted"], 1)
        self.assertEqual(send_pending(self.conn, failed, limit=1)["failed"], 1)
        self.assertEqual(send_pending(self.conn, retry_sent, limit=1)["accepted"], 1)
        self.assertEqual(send_pending(self.conn, timeout, limit=1)["unknown"], 1)
        self.assertEqual(send_pending(self.conn, first, limit=10)["attempted"], 0)
        rows = list_notifications(self.conn)
        self.assertEqual([row["status"] for row in rows], ["ACCEPTED", "ACCEPTED", "UNKNOWN"])
        self.assertEqual([row["attempts"] for row in rows], [1, 2, 1])
        self.assertEqual(rows[1]["sent_at"] is not None, True)
        self.assertEqual(rows[2]["last_error"], "timeout")
        self.assertEqual(len(calls), 4)

    def test_token_expired_pauses_batch_and_probes_queue_head_until_recovered(self):
        self._pending("ev:1", "first alert\nfull detail", "2026-10-20T10:00:00Z")
        self._pending("ev:2", "second alert\nfull detail", "2026-10-20T10:01:00Z")
        self._pending("ev:3", "third alert\nfull detail", "2026-10-20T10:02:00Z")
        calls = []

        def expired(text):
            calls.append(text)
            return "token_expired", "openclaw_token_expired"

        first = send_pending(self.conn, expired, limit=10)
        self.assertEqual(first["token_expired"], 1)
        self.assertEqual(calls, ["first alert\nfull detail"])
        rows = list_notifications(self.conn)
        self.assertEqual([row["status"] for row in rows], ["PENDING", "PENDING", "PENDING"])
        self.assertEqual([row["attempts"] for row in rows], [0, 0, 0])
        self.assertTrue(self.conn.execute("SELECT value FROM meta WHERE key='wechat_paused_at'").fetchone()[0])

        probe_calls = []
        failed_probe = send_pending(self.conn, lambda text: probe_calls.append(text) or ("failed", "prepare failed"), limit=10)
        self.assertEqual(failed_probe["failed"], 1)
        self.assertEqual(probe_calls, ["first alert\nfull detail"])
        self.assertEqual([row["attempts"] for row in list_notifications(self.conn)], [0, 0, 0])
        self.assertIsNotNone(self.conn.execute("SELECT value FROM meta WHERE key='wechat_last_probe_at'").fetchone())

        recovered_calls = []
        recovered = send_pending(self.conn, lambda text: recovered_calls.append(text) or "sent", limit=10)
        self.assertEqual(recovered["accepted"], 3)
        self.assertEqual(len(recovered_calls), 3)
        self.assertEqual(recovered_calls[0], "first alert\nfull detail")
        self.assertIsNone(self.conn.execute("SELECT value FROM meta WHERE key='wechat_paused_at'").fetchone())
        self.assertEqual([row["status"] for row in list_notifications(self.conn)], ["ACCEPTED"] * 3)
        self.assertEqual([row["attempts"] for row in list_notifications(self.conn)], [1, 1, 1])

    def test_ntfy_is_attempted_once_and_failed_notifications_keep_retry_limit(self):
        self._pending("ev:ntfy-1", "first alert\nsecret detail", "2026-10-20T10:00:00Z")
        self._pending("ev:ntfy-2", "second alert\nsecret detail", "2026-10-20T10:01:00Z")
        ntfy_calls = []

        def ntfy(text):
            ntfy_calls.append(text)
            return "sent" if len(ntfy_calls) == 1 else "failed"

        first = send_pending(self.conn, lambda text: ("failed", "send_failed"), limit=2,
                             ntfy_sender=ntfy, ntfy_topic="test-topic")
        self.assertEqual((first["ntfy_sent"], first["ntfy_failed"]), (1, 1))
        rows = list_notifications(self.conn)
        self.assertEqual([row["status"] for row in rows], ["FAILED", "FAILED"])
        self.assertEqual([row["attempts"] for row in rows], [1, 1])
        self.assertEqual([row["ntfy_status"] for row in rows], ["ntfy_sent", "ntfy_failed"])
        second = send_pending(self.conn, lambda text: ("failed", "send_failed"), limit=2,
                              ntfy_sender=ntfy, ntfy_topic="test-topic")
        self.assertEqual(second["failed"], 2)
        self.assertEqual(len(ntfy_calls), 2)
        self.assertEqual([row["attempts"] for row in list_notifications(self.conn)], [2, 2])

    def test_ntfy_failed_fallback_does_not_bypass_three_wechat_attempt_limit(self):
        self._pending("ev:ntfy-retry-limit", "alert\ndetail", "2026-10-20T10:00:00Z")
        wechat_calls = []
        ntfy_calls = []

        for _ in range(4):
            result = send_pending(
                self.conn,
                lambda text: wechat_calls.append(text) or ("failed", "send_failed"),
                limit=1,
                ntfy_sender=lambda text: ntfy_calls.append(text) or "sent",
                ntfy_topic="test-topic",
            )
        self.assertEqual(len(wechat_calls), 3)
        self.assertEqual(len(ntfy_calls), 1)
        self.assertEqual(result["attempted"], 0)
        row = list_notifications(self.conn)[0]
        self.assertEqual((row["status"], row["attempts"], row["ntfy_status"]),
                         ("FAILED", 3, "ntfy_sent"))

    def test_paused_notification_uses_ntfy_once_across_probe_retries(self):
        self._pending("ev:paused-ntfy", "first alert\nsecret detail", "2026-10-20T10:00:00Z")
        ntfy_calls = []
        first = send_pending(self.conn, lambda text: ("token_expired", "expired"), limit=5,
                             ntfy_sender=lambda text: ntfy_calls.append(text) or "sent",
                             ntfy_topic="test-topic")
        self.assertEqual(first["ntfy_sent"], 1)
        self.assertEqual(len(ntfy_calls), 1)
        probe = send_pending(self.conn, lambda text: ("failed", "still paused"), limit=5,
                             ntfy_sender=lambda text: ntfy_calls.append(text) or "sent",
                             ntfy_topic="test-topic")
        self.assertEqual(probe["failed"], 1)
        self.assertEqual(len(ntfy_calls), 1)
        row = list_notifications(self.conn)[0]
        self.assertEqual((row["status"], row["attempts"], row["ntfy_status"]), ("PENDING", 0, "ntfy_sent"))

    def test_missing_ntfy_topic_preserves_original_failed_status(self):
        self._pending("ev:no-ntfy", "alert", "2026-10-20T10:00:00Z")
        with patch.dict("os.environ", {"LABOPS_NTFY_TOPIC": ""}):
            result = send_pending(self.conn, lambda text: ("failed", "send_failed"), limit=1)
        self.assertEqual(result["failed"], 1)
        notification = list_notifications(self.conn)[0]
        self.assertEqual(notification["status"], "FAILED")
        self.assertIsNone(notification["ntfy_status"])

    def test_ntfy_sender_posts_only_first_line_with_required_headers(self):
        with patch("labops.notify._ssl_contexts", return_value=[object()]), \
             patch("labops.notify.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.getcode.return_value = 204
            self.assertEqual(NtfySender("test-topic")("first line\nprivate details\nnext step"), "sent")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.data, b"first line")
        self.assertEqual(request.get_header("Title"), "LabOps")
        self.assertEqual(request.get_header("Tags"), "warning")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 10)

    def test_ntfy_sender_tries_next_ssl_context_after_certificate_error(self):
        with patch("labops.notify._ssl_contexts", return_value=[object(), object()]), \
             patch("labops.notify.urllib.request.urlopen") as urlopen:
            response = urlopen.return_value.__enter__.return_value
            response.getcode.return_value = 200
            urlopen.side_effect = [
                urllib.error.URLError(ssl.SSLError("certificate")),
                urlopen.return_value,
            ]
            self.assertEqual(NtfySender("test-topic")("first line"), "sent")
        self.assertEqual(urlopen.call_count, 2)

    def test_wechat_markdown_templates_and_preserves_existing_list_items(self):
        single = "[LabOps · toy] 单条告警\n任务失败\n- 已有列表项\n下一步：查看日志"
        self.assertEqual(
            _to_wechat_markdown(single),
            "[LabOps · toy] 单条告警\n- 任务失败\n- 已有列表项\n- 下一步：查看日志",
        )
        merged = (
            "[LabOps · 紧急 2 条]\n[LabOps · toy] 告警 A\n详情 A\n"
            "[LabOps · toy] 告警 B\n- 已有列表项 B"
        )
        self.assertEqual(
            _to_wechat_markdown(merged),
            "[LabOps · 紧急 2 条]\n\n[LabOps · toy] 告警 A\n- 详情 A\n\n"
            "[LabOps · toy] 告警 B\n- 已有列表项 B",
        )

    def test_openclaw_sender_formats_template_but_leaves_llm_rewrite_unchanged(self):
        template = "[LabOps · toy] 模板告警\n模板详情\n下一步：查看任务"
        self._pending("ev:wechat-template", template, self.timestamp)
        with patch.object(OpenClawSender, "__call__", autospec=True, return_value="sent") as sender:
            send_pending(self.conn, OpenClawSender())
        self.assertEqual(sender.call_args.args[1], _to_wechat_markdown(template))

        notify_config(self.conn, llm_rewrite="on")
        self._pending("ev:wechat-llm", template, self.timestamp)
        rewritten = "改写正文\n仍保留单换行"
        with patch("labops.rewrite.rewrite", return_value=(rewritten, "llm")), \
             patch.object(OpenClawSender, "__call__", autospec=True, return_value="sent") as sender:
            send_pending(self.conn, OpenClawSender())
        self.assertEqual(sender.call_args.args[1], rewritten)

    def test_alert_detail_is_independent_from_confidential_setting(self):
        set_confidential(self.conn, "toy", True)
        head = commit_file(self.repo, "notify-result.txt", "change\n")
        self.conn.execute("UPDATE rounds SET head_commit=? WHERE round_id=?", (head, self.round_id))
        self._event("round", self.round_id, "BLOCKED", {"reason": "verify_path_exists"})
        scan(self.conn)
        message = list_notifications(self.conn)[0]["text"]
        self.assertIn("[LabOps · toy] notify r1 · LabOps 验证受阻", message)
        self.assertIn("原因 verify_path_exists", message)
        self.assertEqual(self.conn.execute("SELECT alert_detail FROM repos WHERE repo_id='toy'").fetchone()[0], 1)

        set_alert_detail(self.conn, "toy", False)
        self._event("round", self.round_id, "BLOCKED", {"reason": "another detail"})
        scan(self.conn)
        short_message = list_notifications(self.conn)[1]["text"]
        self.assertEqual(short_message, "[LabOps · toy] notify r1 · LabOps 验证受阻")

    def test_completion_events_create_four_notifications(self):
        head = commit_file(self.repo, "completion.txt", "done\n")
        self.conn.execute("UPDATE rounds SET head_commit=? WHERE round_id=?", (head, self.round_id))
        self._event("round", self.round_id, "CLAIMED")
        self._event("round", self.round_id, "VERIFIED")
        self.conn.execute(
            "INSERT INTO reviews(review_id,task_id,round_id,reviewer,state,verdict,independence,base_commit,reviewed_commit,reviewed_tree,scope_id,acceptance_profile_hash,findings_json,created_at,updated_at) "
            "VALUES('rv-complete',?,?,'opus','PASS','PASS','INDEPENDENT',?,?,?,'scope',?,'[]',?,?)",
            (self.task_id, self.round_id, self.base, self.base, "tree", self.profile_hash,
             self.timestamp, self.timestamp),
        )
        self._event("review", "rv-complete", "PASS")
        self.conn.execute(
            "INSERT INTO integrations(integration_id,task_id,candidate_commit,candidate_tree,main_before,state,check_json,created_at,updated_at) "
            "VALUES('ig-complete',?,?,?,?, 'MERGED','{}',?,?)",
            (self.task_id, self.base, "tree", self.base, self.timestamp, self.timestamp),
        )
        self._event("integration", "ig-complete", "MERGED")

        self.assertEqual(scan(self.conn), 4)
        rows = {row["kind"]: row["text"] for row in list_notifications(self.conn)}
        self.assertEqual(set(rows), {"submitted", "verified", "review_passed", "merged"})
        self.assertEqual(rows["submitted"],
                         f"[LabOps · toy] notify r1 · Codex 已提交\ncommit {head[:8]}，改动 1 个文件\n"
                         f"下一步：LabOps 验证（labops verify --round {self.round_id}）")
        self.assertEqual(rows["verified"],
                         "[LabOps · toy] notify r1 · LabOps 验证通过\n验收命令 全部 exit 0，用时 0.0s\n"
                         "下一步：转给 Opus 审查\n任务：notify test")
        self.assertEqual(rows["review_passed"],
                         "[LabOps · toy] notify r1 · Opus 审查通过\n下一步：等你批准合并")
        self.assertEqual(rows["merged"],
                         f"[LabOps · toy] {self.task_id} r1 · LabOps 已合入 main\n{self.base[:8]}")

    def test_confidential_completion_text_uses_alert_detail_setting(self):
        set_confidential(self.conn, "toy", True)
        self._event("round", self.round_id, "CLAIMED")
        self._event("round", self.round_id, "VERIFIED")
        self.conn.execute(
            "INSERT INTO reviews(review_id,task_id,round_id,reviewer,state,verdict,independence,base_commit,reviewed_commit,reviewed_tree,scope_id,acceptance_profile_hash,findings_json,created_at,updated_at) "
            "VALUES('rv-private-complete',?,?,'opus','PASS','PASS','SELF_REVIEWED_FIX',?,?,?,'scope',?,'[]',?,?)",
            (self.task_id, self.round_id, self.base, self.base, "tree", self.profile_hash,
             self.timestamp, self.timestamp),
        )
        self._event("review", "rv-private-complete", "PASS")
        self.conn.execute(
            "INSERT INTO integrations(integration_id,task_id,candidate_commit,candidate_tree,main_before,state,check_json,created_at,updated_at) "
            "VALUES('ig-private-complete',?,?,?,?, 'MERGED','{}',?,?)",
            (self.task_id, self.base, "tree", self.base, self.timestamp, self.timestamp),
        )
        self._event("integration", "ig-private-complete", "MERGED")
        self.assertEqual(scan(self.conn), 4)
        rows = {row["kind"]: row["text"] for row in list_notifications(self.conn)}
        self.assertEqual(self.conn.execute("SELECT alert_detail FROM repos WHERE repo_id='toy'").fetchone()[0], 1)
        self.assertTrue(all(text.startswith("[LabOps · toy]") for text in rows.values()))
        self.assertIn("labops approve self-review", rows["review_passed"])
        set_alert_detail(self.conn, "toy", False)
        self._event("round", self.round_id, "CLAIMED")
        scan(self.conn)
        submitted = [row["text"] for row in list_notifications(self.conn) if row["kind"] == "submitted"]
        self.assertIn("[LabOps · toy] notify r1 · Codex 已提交", submitted)

    def test_transfer_alert_appends_cleaned_task_goal_and_detail_off_hides_it(self):
        from labops.notify import _event_notification

        self.conn.execute(
            "UPDATE tasks SET goal=? WHERE task_id=?",
            ("Investigate background run failures and keep the retry path stable (see current-plan.md)", self.task_id),
        )
        event_id = self._event("round", self.round_id, "REJECTED")
        event = self.conn.execute("SELECT * FROM events WHERE seq=?", (event_id,)).fetchone()
        notification = _event_notification(self.conn, event)
        self.assertIn("下一步：转给 Codex：调用 next_task 返工", notification["text"])
        goal_prefix = "Investigate background run failures and keep the retry path stable"[:40]
        self.assertIn(f"任务：{goal_prefix}", notification["text"])
        self.assertNotIn("(see current-plan.md)", notification["text"])

        set_confidential(self.conn, "toy", True)
        set_alert_detail(self.conn, "toy", False)
        notification = _event_notification(self.conn, event)
        self.assertEqual(len(notification["text"].splitlines()), 1)
        self.assertNotIn("任务：", notification["text"])

    def test_opus_round_hint_and_self_review_command(self):
        self.conn.execute("UPDATE rounds SET actor='opus' WHERE round_id=?", (self.round_id,))
        self._event("round", self.round_id, "CLAIMED")
        self._event("round", self.round_id, "VERIFIED")
        self.assertEqual(scan(self.conn), 2)
        verified = next(row["text"] for row in list_notifications(self.conn) if row["kind"] == "verified")
        self.assertEqual(verified,
                         "[LabOps · toy] notify r1 · LabOps 验证通过\n验收命令 全部 exit 0，用时 0.0s\n"
                         "下一步：转给 Codex 或你审查\n任务：notify test")

        self.conn.execute(
            "INSERT INTO reviews(review_id,task_id,round_id,reviewer,state,verdict,independence,base_commit,reviewed_commit,reviewed_tree,scope_id,acceptance_profile_hash,findings_json,created_at,updated_at) "
            "VALUES('rv-self-notify',?,?,'opus','PASS','PASS','SELF_REVIEWED_FIX',?,?,?,'scope',?,'[]',?,?)",
            (self.task_id, self.round_id, self.base, self.base, "tree", self.profile_hash,
             self.timestamp, self.timestamp),
        )
        seq = self._event("review", "rv-self-notify", "PASS")
        event = self.conn.execute("SELECT * FROM events WHERE seq=?", (seq,)).fetchone()
        result = _event_notification(self.conn, event)
        self.assertEqual(result["text"],
                         f"[LabOps · toy] notify r1 · 自审通过\n下一步：需你接受自审："
                         f"labops approve self-review --task-id {self.task_id}")

    def test_pass_with_notes_uses_review_passed_notification(self):
        self.conn.execute(
            "INSERT INTO reviews(review_id,task_id,round_id,reviewer,state,verdict,independence,base_commit,reviewed_commit,reviewed_tree,scope_id,acceptance_profile_hash,findings_json,created_at,updated_at) "
            "VALUES('rv-notes',?,?,'opus','PASS_WITH_NOTES','PASS_WITH_NOTES','INDEPENDENT',?,?,?,'scope',?,'[]',?,?)",
            (self.task_id, self.round_id, self.base, self.base, "tree", self.profile_hash,
             self.timestamp, self.timestamp),
        )
        seq = self._event("review", "rv-notes", "PASS_WITH_NOTES")
        result = _event_notification(self.conn, self.conn.execute("SELECT * FROM events WHERE seq=?", (seq,)).fetchone())
        self.assertEqual(result["kind"], "review_passed")
        self.assertEqual(result["text"], "[LabOps · toy] notify r1 · Opus 审查通过\n下一步：等你批准合并")

    def test_muted_kind_skips_scan_and_suppresses_pending_send(self):
        notify_config(self.conn, urgent_kinds=["submitted"])
        self._event("round", self.round_id, "CLAIMED")
        self.assertEqual(scan(self.conn), 1)
        self.assertTrue(mute_kind(self.conn, "submitted")["muted"])
        self._event("round", self.round_id, "CLAIMED")
        self.assertEqual(scan(self.conn), 0)
        sent = []
        self.assertEqual(send_pending(self.conn, lambda text: sent.append(text) or "sent")["attempted"], 0)
        self.assertTrue(unmute_kind(self.conn, "submitted")["changed"])
        self.assertEqual(send_pending(self.conn, lambda text: sent.append(text) or "sent")["accepted"], 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(list_notifications(self.conn)), 1)

    def test_openclaw_sender_parses_delivery_status_without_calling_real_client(self):
        executable = r"C:\tools\npm\openclaw.CMD"
        with patch.dict("os.environ", {"LABOPS_NOTIFY_ACCOUNT": "acct", "LABOPS_NOTIFY_TARGET": "target"}), \
             patch("labops.notify._openclaw_command", return_value=[executable]), \
             patch("labops.notify.subprocess.run") as run:
            run.return_value.stdout = '{"deliveryStatus":"sent"}'
            run.return_value.stderr = ""
            run.return_value.returncode = 0
            self.assertEqual(OpenClawSender()("hello"), ("sent", None))
            argv = run.call_args.args[0]
            self.assertEqual(argv[:6], [executable, "message", "send", "--json", "--channel", "openclaw-weixin"])
            self.assertEqual(run.call_args.kwargs["timeout"], 180.0)

    def test_openclaw_sender_reads_nested_delivery_status(self):
        # SEC-3: real openclaw 2026.10.5 --json output nests deliveryStatus under "payload".
        stdout = ('{"action":"send","channel":"openclaw-weixin","dryRun":false,"payload":{"channel":"openclaw-weixin",'
                  '"deliveryStatus":"sent","payloadOutcomes":[{"index":0,"status":"sent","resultCount":1}]}}')
        for nested, expected in (("sent", ("sent", None)), ("failed", ("failed", "failed"))):
            with patch.dict("os.environ", {"LABOPS_NOTIFY_ACCOUNT": "acct", "LABOPS_NOTIFY_TARGET": "target"}), \
                 patch("labops.notify.shutil.which", return_value="openclaw.CMD"), \
                 patch("labops.notify.subprocess.run") as run:
                run.return_value.stdout = stdout.replace('"deliveryStatus":"sent"', f'"deliveryStatus":"{nested}"')
                run.return_value.stderr = ""
                run.return_value.returncode = 0
                self.assertEqual(OpenClawSender()("hello"), expected)

    def test_openclaw_cmd_shim_is_bypassed_so_newlines_survive(self):
        from labops.notify import _openclaw_command
        with tempfile.TemporaryDirectory() as temporary:
            npm = Path(temporary)
            (npm / "openclaw.CMD").write_text("@echo off", encoding="utf-8")
            entry = npm / "node_modules" / "openclaw" / "openclaw.mjs"
            entry.parent.mkdir(parents=True)
            entry.write_text("", encoding="utf-8")

            def which(name):
                return {"openclaw": str(npm / "openclaw.CMD"), "node": "C:/nodejs/node.exe"}.get(name)

            with patch("labops.notify.shutil.which", side_effect=which):
                self.assertEqual(_openclaw_command(), ["C:/nodejs/node.exe", str(entry)])
                with patch.dict("os.environ", {"LABOPS_NOTIFY_ACCOUNT": "acct", "LABOPS_NOTIFY_TARGET": "target"}), \
                     patch("labops.notify.subprocess.run") as run:
                    run.return_value.stdout = '{"payload": {"deliveryStatus": "sent"}}'
                    run.return_value.stderr = ""
                    run.return_value.returncode = 0
                    self.assertEqual(OpenClawSender()("line one\nline two"), ("sent", None))
                    argv = run.call_args.args[0]
                    self.assertEqual(argv[:2], ["C:/nodejs/node.exe", str(entry)])
                    self.assertLess(argv.index("--json"), argv.index("-m"))
                    self.assertEqual(argv[-1], "line one\nline two")
            entry.unlink()
            with patch("labops.notify.shutil.which", side_effect=which):
                self.assertEqual(_openclaw_command(), [str(npm / "openclaw.CMD")])

    def test_notify_timeout_default_and_override(self):
        from labops.notify import _notify_timeout
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("LABOPS_NOTIFY_TIMEOUT", None)
            self.assertEqual(_notify_timeout(), 180.0)
        for raw, expected in (("90", 90.0), ("bad", 180.0), ("0", 180.0), ("99999", 180.0)):
            with patch.dict("os.environ", {"LABOPS_NOTIFY_TIMEOUT": raw}):
                self.assertEqual(_notify_timeout(), expected)

    def test_openclaw_sender_skips_ansi_log_lines_before_json(self):
        # Observed 2026-10-20 after a gateway restart: log lines precede the pretty-printed JSON.
        esc = chr(27)
        stdout = (f"{esc}[33m[state/agent-db]{esc}[39m agent database integrity gate\n"
                  '{\n  "action": "send",\n  "payload": {\n    "deliveryStatus": "sent"\n  }\n}\n'
                  f"{esc}[33m[state/agent-db]{esc}[39m clean-close receipt {{not json}}\n")
        with patch.dict("os.environ", {"LABOPS_NOTIFY_ACCOUNT": "acct", "LABOPS_NOTIFY_TARGET": "target"}), \
             patch("labops.notify.shutil.which", return_value="openclaw.CMD"), \
             patch("labops.notify.subprocess.run") as run:
            run.return_value.stdout = stdout
            run.return_value.stderr = ""
            run.return_value.returncode = 0
            self.assertEqual(OpenClawSender()("hello"), ("sent", None))
            run.return_value.stdout = f"{esc}[33mlog only{esc}[39m\n{{broken\n"
            self.assertEqual(OpenClawSender()("hello"), ("unknown", "unparseable_delivery_status"))

    def test_openclaw_sender_classifies_expired_credentials_from_stdout_or_stderr(self):
        outputs = (("", "ret=-2"), ("prepare FAILED while sending", ""))
        for stdout, stderr in outputs:
            with self.subTest(stdout=stdout, stderr=stderr), \
                 patch.dict("os.environ", {"LABOPS_NOTIFY_ACCOUNT": "acct", "LABOPS_NOTIFY_TARGET": "target"}), \
                 patch("labops.notify.shutil.which", return_value="openclaw.CMD"), \
                 patch("labops.notify.subprocess.run") as run:
                run.return_value.stdout = stdout
                run.return_value.stderr = stderr
                run.return_value.returncode = 1
                self.assertEqual(OpenClawSender()("hello"), ("token_expired", "openclaw_token_expired"))

    def test_openclaw_sender_reports_missing_executable_without_spawning(self):
        with patch.dict("os.environ", {"LABOPS_NOTIFY_ACCOUNT": "acct", "LABOPS_NOTIFY_TARGET": "target"}), \
             patch("labops.notify.shutil.which", return_value=None), \
             patch("labops.notify.subprocess.run") as run:
            self.assertEqual(OpenClawSender()("hello"), ("failed", "openclaw_not_found"))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
