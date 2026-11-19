import hashlib
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from labops import desktop, desktop_codex, notify, rewrite
from labops.config import now
from labops.errors import IdempotencyConflict, ValidationError
from tests.test_desktop import (HOST_ID, PROMPT, TOKEN, ClaudeSink, FakeCodexStream, append_jsonl,
                                desktop_fixture, rollout_event, transcript_record)


class WeChatDispatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = self.enterContext(desktop_fixture())
        self.conn = self.fixture.conn
        self.sink = ClaudeSink()
        self.start = "2026-11-19T12:00:00Z"

    def dispatch(self, request_id="request-123", prompt=PROMPT):
        return desktop.desktop_dispatch(
            self.conn, kind="claude", target=HOST_ID, prompt=prompt, request_id=request_id, actor="secretary",
            process_info=self.fixture.process_info, claude_open=lambda _: self.sink,
        )

    def scan(self, minutes=0):
        timestamp = (datetime.fromisoformat(self.start) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return notify.scan_desktop_replies(self.conn, now_ts=timestamp, process_info=self.fixture.process_info)

    def mark_created(self):
        self.conn.execute("UPDATE desktop_requests SET created_at=?", (self.start,))

    def complete_claude(self):
        self.dispatch()
        content = json.loads(self.sink.payload.splitlines()[1])["message"]["content"]
        append_jsonl(self.fixture.transcript, transcript_record("user", content),
                     transcript_record("assistant", "验收通过"))

    def outbox(self):
        return self.conn.execute("SELECT * FROM notifications ORDER BY event_key").fetchall()

    def test_invalid_origin(self):
        with self.assertRaisesRegex(ValidationError, "invalid_origin"):
            self.fixture.send("claude", origin="bogus")

    def test_wechat_prefix_and_original_prompt_hash(self):
        result = self.dispatch()
        content = json.loads(self.sink.payload.splitlines()[1])["message"]["content"]
        self.assertEqual(content, f"[labops:request-123]\n{desktop.WECHAT_PREFIX}\n{PROMPT}")
        row = self.conn.execute("SELECT prompt_sha256,prompt_chars,origin,actor FROM desktop_requests").fetchone()
        self.assertEqual(tuple(row), (hashlib.sha256(PROMPT.encode()).hexdigest(), len(PROMPT), "wechat", "secretary"))
        self.assertNotIn("detail", result)
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_replay_cannot_change_origin(self):
        self.fixture.send("claude", claude_open=lambda _: self.sink)
        with self.assertRaises(IdempotencyConflict):
            self.dispatch()

    def test_dispatch_length_limit(self):
        with self.assertRaisesRegex(ValidationError, "prompt_too_long") as exc:
            self.dispatch(prompt="a" * 4001)
        self.assertEqual(exc.exception.detail, {"chars": 4001, "max": 4000})
        with self.assertRaisesRegex(ValidationError, "prompt_empty"):
            self.dispatch(prompt=" \n")

    def test_dispatch_rate_limit_exempts_replay(self):
        self.dispatch()
        self.conn.executemany(
            "INSERT INTO desktop_requests(request_id,kind,target,actor,prompt_sha256,prompt_chars,"
            "state,detail_json,created_at,updated_at,origin) VALUES(?,'claude','fixture','secretary',"
            "'sha',1,'FAILED','{}',?,?,'wechat')",
            [(f"fixture-{index}", now(), now()) for index in range(19)],
        )
        with self.assertRaisesRegex(ValidationError, "dispatch_rate_limited"):
            self.dispatch(request_id="request-456")
        self.assertTrue(self.dispatch()["replayed"])

    def test_secretary_view_redacts_confidential_cwd_and_reply(self):
        self.conn.execute("INSERT INTO repos(repo_id,path,confidential,created_at) VALUES('private',?,1,'t')",
                          (str(self.fixture.root),))
        listed = desktop.desktop_list(self.conn, kind="claude", process_info=self.fixture.process_info)
        item = desktop.secretary_view(self.conn, listed)["sessions"][0]
        self.assertEqual((item["cwd"], item["name"], item["confidential"]), (None, None, True))
        self.complete_claude()
        safe = desktop.secretary_view(self.conn, self.fixture.status())
        self.assertEqual((safe["name"], safe["reply"], safe["confidential"]), (None, None, True))
        self.assertNotIn("detail", safe)

    def test_completed_reply_is_enqueued_once(self):
        self.complete_claude()
        self.mark_created()
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.scan(), 0)
        self.assertEqual(len(self.outbox()), 1)
        report = self.outbox()[0]
        self.assertEqual((report["kind"], report["event_key"]), ("desktop_reply", "desktop:request-123"))
        self.assertIn("验收通过", report["text"])
        row = self.conn.execute("SELECT reported_at,reported_phase FROM desktop_requests").fetchone()
        self.assertEqual(tuple(row), (self.start, "completed"))

    def test_missing_delivery_and_unknown_report_after_fifteen_minutes(self):
        self.dispatch()
        self.mark_created()
        self.assertEqual(self.scan(14), 0)
        self.assertEqual(self.scan(15), 1)
        self.assertIn("未送达", self.outbox()[0]["text"])
        self.conn.execute("DELETE FROM notifications")
        self.conn.execute("UPDATE desktop_requests SET state='UNKNOWN',reported_at=NULL,reported_phase=NULL")
        self.assertEqual(self.scan(14), 0)
        self.assertEqual(self.scan(15), 1)
        self.assertIn("发送结果未知", self.outbox()[0]["text"])

    def test_running_codex_is_reported_at_timeout(self):
        stream = FakeCodexStream()
        self.fixture.send("codex", origin="wechat", codex_connect=lambda _: desktop_codex.IpcClient(stream))
        self.mark_created()
        append_jsonl(self.fixture.rollout, rollout_event("task_started"))
        self.assertEqual(self.scan(359), 0)
        self.assertEqual(self.scan(360), 1)
        self.assertIn("超时未完成", self.outbox()[0]["text"])

    def test_failed_is_reported_sending_and_mcp_are_skipped(self):
        self.dispatch()
        self.mark_created()
        self.conn.execute("UPDATE desktop_requests SET state='SENDING'")
        self.assertEqual(self.scan(999), 0)
        self.conn.execute("UPDATE desktop_requests SET state='FAILED',origin='mcp'")
        self.assertEqual(self.scan(999), 0)
        self.conn.execute("UPDATE desktop_requests SET origin='wechat'")
        self.assertEqual(self.scan(), 1)
        self.assertIn("发送失败", self.outbox()[0]["text"])

    def test_confidential_report_and_status_read_failure(self):
        self.complete_claude()
        self.mark_created()
        self.conn.execute("INSERT INTO repos(repo_id,path,confidential,created_at) VALUES('private',?,1,'t')",
                          (str(self.fixture.root),))
        with patch("labops.desktop.desktop_status", side_effect=OSError("fixture read failure")):
            self.assertEqual(self.scan(), 0)
        self.assertIsNone(self.conn.execute("SELECT reported_at FROM desktop_requests").fetchone()[0])
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.outbox()[0]["text"], "[LabOps · 派活回报] Claude · （机密仓库） · 已完成\n"
                                                  "请求：request-123\n正文不经微信发送；在电脑上用 desktop_status 查看。")

    def test_notify_user_validation_replay_and_per_actor_rate(self):
        def report(index, text="  报告通过  ", actor="codex"):
            return notify.notify_user(self.conn, actor=actor, text=text, request_id=f"report-{index:02}",
                                      now_ts=self.start)

        self.assertTrue(report(0)["queued"])
        self.assertEqual(self.outbox()[0]["text"], "[LabOps · Codex 回报]\n报告通过")
        self.assertTrue(report(0, "different")["replayed"])
        self.assertEqual(len(self.outbox()), 1)
        for body, actor, error in ((" ", "codex", "report_text_empty"),
                                   ("x" * 1501, "codex", "report_text_too_long"),
                                   ("x", "secretary", "actor_tool_not_allowed")):
            with self.subTest(error=error), self.assertRaisesRegex(ValidationError, error):
                report(1, body, actor)
        for index in range(1, 30):
            report(index)
        with self.assertRaisesRegex(ValidationError, "report_rate_limited"):
            report(30)
        self.assertTrue(report(0)["replayed"])
        self.assertTrue(report(30, actor="opus")["queued"])

    def test_direct_kinds_bypass_digest_and_rewrite_and_run_reports_count(self):
        self.complete_claude()
        self.mark_created()
        self.assertEqual(self.scan(), 1)
        notify.notify_user(self.conn, actor="codex", text="报告通过", request_id="report-99", now_ts=self.start)
        self.conn.execute("INSERT INTO meta VALUES('notify_urgent_kinds','[\"verify_failed\"]')")
        self.assertEqual(notify.merge_pending(self.conn, now_ts="2026-11-19T23:00:00Z"),
                         {"merged_urgent": 0, "merged_digest": 0})
        self.assertEqual(len(notify._pending_rows(self.conn, 10, [])), 2)
        self.conn.execute("UPDATE meta SET value=? WHERE key='notify_urgent_kinds'",
                          ('["desktop_reply","agent_report"]',))
        sent = []
        with patch("labops.rewrite.rewrite") as rewrite, patch(
                "labops.desktop.runs._process_info", self.fixture.process_info):
            result = notify.run(self.conn, sender=lambda text: sent.append(text) or "sent", ntfy_topic="")
        rewrite.assert_not_called()
        self.assertEqual((result["desktop_reported"], result["accepted"]), (0, 2))
        self.assertCountEqual(sent, [row["text"] for row in self.outbox()])

    def message_claude(self, request_id="codex-note", *, actor="codex", prompt=PROMPT, opener=None):
        return desktop.message_claude(
            self.conn, target=HOST_ID, prompt=prompt, request_id=request_id, actor=actor,
            process_info=self.fixture.process_info, claude_open=opener or (lambda _: self.sink),
        )

    def test_message_claude_prefix_actor_and_rate_limit(self):
        with self.assertRaisesRegex(ValidationError, "actor_tool_not_allowed"):
            self.message_claude(actor="opus")
        self.message_claude()
        content = json.loads(self.sink.payload.splitlines()[1])["message"]["content"]
        self.assertEqual(content, f"[labops:codex-note]\n{desktop.CODEX_PREFIX}\n{PROMPT}")
        self.assertEqual(self.conn.execute("SELECT origin FROM desktop_requests").fetchone()[0], "codex")
        self.conn.executemany(
            "INSERT INTO desktop_requests(request_id,kind,target,actor,prompt_sha256,prompt_chars,"
            "state,detail_json,created_at,updated_at,origin) VALUES(?,'claude','fixture','codex',"
            "'sha',1,'FAILED','{}',?,?,'codex')",
            [(f"limit-{index}", now(), now()) for index in range(19)],
        )
        with self.assertRaisesRegex(ValidationError, "message_rate_limited"):
            self.message_claude("another-note")
        self.assertTrue(self.message_claude()["replayed"])

    def test_relay_only_queues_sent_non_wechat_and_redacts_confidential_text(self):
        stream = FakeCodexStream()
        self.fixture.send("codex", request_id="opus-note", codex_connect=lambda _: desktop_codex.IpcClient(stream))
        self.message_claude()
        self.assertTrue(self.message_claude()["replayed"])
        self.sink = ClaudeSink()
        self.dispatch(request_id="user-note")

        def unavailable(_):
            raise OSError("fixture")
        with self.assertRaisesRegex(ValidationError, "pipe_unavailable"):
            self.message_claude("failed-note", opener=unavailable)
        self.conn.execute("INSERT INTO repos(repo_id,path,confidential,created_at) VALUES('private',?,1,'t')",
                          (str(self.fixture.root),))
        self.message_claude("private-note", prompt="fixture secret", opener=lambda _: ClaudeSink())
        rows = {row["event_key"]: row["text"] for row in self.outbox() if row["kind"] == "agent_relay"}
        self.assertEqual(set(rows), {"relay:opus-note", "relay:codex-note", "relay:private-note"})
        self.assertEqual(rows["relay:opus-note"], f"[LabOps · Claude → Codex] 2d3404de\n请求：opus-note\n{PROMPT}")
        self.assertEqual(rows["relay:codex-note"], f"[LabOps · Codex → Claude] desk\n请求：codex-note\n{PROMPT}")
        self.assertEqual(rows["relay:private-note"],
                         "[LabOps · Codex → Claude] （机密仓库）\n请求：private-note\n（机密仓库，正文不经微信）")

    def test_summarize_preserves_required_terms_and_falls_back(self):
        header = "[LabOps · Codex → Claude] desk"
        body = "请求：codex-note\nT20261119-labops-wxdispatch Q1 src/labops/desktop.py " + "长" * 100
        text = header + "\n" + body
        valid = "codex-note T20261119-labops-wxdispatch Q1 src/labops/desktop.py"
        fallback = header + "\n" + body[:79] + "…"
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}):
            self.assertEqual(rewrite.summarize(text, enabled=True, confidential=False, max_chars=80),
                             (fallback, "template_disabled"))
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "fixture-key"}), patch(
                "labops.rewrite._request_completion", return_value=valid) as completion:
            self.assertEqual(rewrite.summarize(text, enabled=True, confidential=False, max_chars=80),
                             (header + "\n" + valid, "llm"))
            self.assertEqual(completion.call_args.args, (body,))
            self.assertIn("不超过 80 字", completion.call_args.kwargs["system"])
            for term in ("T20261119-labops-wxdispatch", "Q1", "codex-note", "src/labops/desktop.py"):
                with self.subTest(term=term):
                    completion.return_value = valid.replace(term, "")
                    self.assertEqual(rewrite.summarize(text, enabled=True, confidential=False, max_chars=80),
                                     (fallback, "template_check_failed"))
            completion.side_effect = OSError("fixture")
            self.assertEqual(rewrite.summarize(text, enabled=True, confidential=False, max_chars=80),
                             (fallback, "template_error"))

    def test_default_rewrite_payload_is_unchanged(self):
        with patch.dict("os.environ", {"LABOPS_DS_MODEL": "deepseek-flash"}):
            payload = rewrite._request_payload("fixture")
        self.assertEqual(payload, {
            "model": "deepseek-flash",
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": (
                    "你负责改写给研究者本人看的微信推送。使用中文和紧凑分段，每条事件单独成段；"
                    "不增删事实；模板中的任务 ID、round、integration ID、SHA、下一步命令、带单位数字、"
                    "run 目录名必须逐字保留；总长不超过原文的 1.2 倍且不超过 800 字；只输出正文。"
                )},
                {"role": "user", "content": "fixture"},
            ],
        })

    def test_relay_sends_with_summary_and_reuses_stored_text(self):
        self.message_claude()
        self.conn.execute("INSERT INTO meta VALUES('notify_urgent_kinds','[\"verify_failed\"]')")
        self.assertEqual(notify.merge_pending(self.conn), {"merged_urgent": 0, "merged_digest": 0})
        self.assertEqual([row["event_key"] for row in notify._pending_rows(self.conn, 10, [])], ["relay:codex-note"])
        sent = []
        with patch("labops.rewrite.summarize", return_value=("fixture summary", "llm")) as summarize, patch(
                "labops.rewrite.rewrite") as normal_rewrite:
            first = notify.run(self.conn, sender=lambda text: sent.append(text) or "sent", ntfy_topic="")
            row = self.outbox()[0]
            cached = notify._prepare_send_text(self.conn, row["event_key"], row["text"])
        self.assertEqual((first["accepted"], sent, cached), (1, ["fixture summary"], "fixture summary"))
        self.assertEqual((row["rewritten_text"], row["rewrite_outcome"]), ("fixture summary", "llm"))
        summarize.assert_called_once_with(row["text"], enabled=False, confidential=False, max_chars=400)
        normal_rewrite.assert_not_called()

    def test_codex_worker_desktop_list_is_claude_only(self):
        from labops import mcp_worker

        with self.assertRaisesRegex(ValidationError, "kind_not_allowed"):
            mcp_worker._dispatch("desktop_list", {"kind": "codex"}, "codex")
        with patch("labops.desktop.runs._process_info", self.fixture.process_info):
            listed = mcp_worker._dispatch("desktop_list", {"kind": "claude"}, "codex")
        self.assertEqual(listed["sessions"][0]["host_session_id"], HOST_ID)

    def test_secretary_ambiguous_target_error_redacts_confidential_candidates(self):
        from labops import mcp_worker

        self.conn.execute("INSERT INTO repos(repo_id,path,confidential,created_at) VALUES('private',?,1,'t')",
                          (str(self.fixture.root),))
        public_cwd = str(self.fixture.root.with_name(self.fixture.root.name + "-public"))
        second = dict(self.fixture.registration, cwd=public_cwd, sessionId="ae234889-fb4d-4dde-8bae-d939b411d772")
        (self.fixture.sessions / "second.json").write_text(json.dumps(second), encoding="utf-8")
        with patch("labops.desktop.runs._process_info", self.fixture.process_info):
            with self.assertRaisesRegex(ValidationError, "target_ambiguous") as raised:
                mcp_worker._dispatch("desktop_dispatch", {
                    "kind": "claude", "target": "desk", "prompt": PROMPT, "request_id": "request-error",
                }, "secretary")
        private, public = raised.exception.detail["candidates"]
        self.assertEqual((private["name"], private["cwd"], private["confidential"]), (None, None, True))
        self.assertEqual((public["name"], public["cwd"]), ("desk", public_cwd))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM desktop_requests").fetchone()[0], 0)


