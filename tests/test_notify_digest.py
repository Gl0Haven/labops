import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops.db import connect, migrate
from labops.notify import merge_pending, notify_config, run, send_pending


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LABOPS_HOME": str(Path(self.temp.name) / "home")})
        self.env.start()
        os.environ.pop("LABOPS_NTFY_TOPIC", None)
        self.conn = connect(Path(self.temp.name) / "home" / "labops.db")
        migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def _pending(self, key, kind, text, created_at):
        self.conn.execute("INSERT INTO notifications(event_key,kind,text,status,created_at) VALUES(?,?,?,'PENDING',?)",
                          (key, kind, text, created_at))

    def _statuses(self):
        return dict(self.conn.execute("SELECT event_key,status FROM notifications").fetchall())

    def test_single_urgent_goes_out_alone_and_routine_waits(self):
        self._pending("ev:1", "verify_failed", "[LabOps · toy] a r1 · 验证失败\n要点\n下一步：修", "2026-10-20T10:00:00Z")
        self._pending("ev:2", "submitted", "[LabOps · toy] a r1 · 已提交\n下一步：验证", "2026-10-20T10:00:00Z")
        self.assertEqual(merge_pending(self.conn, now_ts="2026-10-20T10:30:00Z"), {"merged_urgent": 0, "merged_digest": 0})
        sent = []
        counts = send_pending(self.conn, lambda text: sent.append(text) or "sent")
        self.assertEqual(counts["accepted"], 1)
        self.assertEqual(sent, ["[LabOps · toy] a r1 · 验证失败\n要点\n下一步：修"])
        self.assertEqual(self._statuses()["ev:2"], "PENDING")

    def test_several_urgent_alerts_become_one_message_with_full_text(self):
        for index in range(3):
            self._pending(f"ev:{index}", "awaiting_approval", f"[LabOps · toy] t{index} · 等你批准\n下一步：approve {index}",
                          f"2026-10-20T10:0{index}:00Z")
        self.assertEqual(merge_pending(self.conn, now_ts="2026-10-20T10:05:00Z")["merged_urgent"], 3)
        sent = []
        send_pending(self.conn, lambda text: sent.append(text) or "sent")
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("[LabOps · 紧急 3 条"))
        self.assertIn("下一步：approve 2", sent[0])
        statuses = self._statuses()
        self.assertEqual([statuses[f"ev:{i}"] for i in range(3)], ["MERGED"] * 3)

    def test_digest_waits_for_window_then_sends_first_lines_once(self):
        notify_config(self.conn, digest_minutes=60)
        for index in range(12):
            self._pending(f"ev:{index:02d}", "merged", f"[LabOps · toy] t{index} · 已合入\n第二行细节 {index}",
                          f"2026-10-20T10:{index:02d}:00Z")
        self.assertEqual(merge_pending(self.conn, now_ts="2026-10-20T10:30:00Z")["merged_digest"], 0)
        self.assertEqual(merge_pending(self.conn, now_ts="2026-10-20T11:00:00Z")["merged_digest"], 12)
        sent = []
        send_pending(self.conn, lambda text: sent.append(text) or "sent")
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("[LabOps · 汇总 12 条"))
        self.assertNotIn("第二行细节", sent[0])
        self.assertLessEqual(len(sent[0]), 700)
        self.assertEqual(merge_pending(self.conn, now_ts="2026-10-20T13:00:00Z")["merged_digest"], 0)

    def test_batch_reuses_pause_path_and_sources_stay_merged(self):
        notify_config(self.conn, digest_minutes=0)
        self._pending("ev:1", "merged", "[LabOps · toy] a · 已合入", "2026-10-20T10:00:00Z")
        self._pending("ev:2", "verified", "[LabOps · toy] b · 验证通过", "2026-10-20T10:01:00Z")
        counts = run(self.conn, sender=lambda text: "token_expired")
        self.assertEqual(counts["merged_digest"], 2)
        self.assertEqual(counts["token_expired"], 1)
        batch = self.conn.execute("SELECT status,attempts FROM notifications WHERE kind='batch_digest'").fetchone()
        self.assertEqual(tuple(batch), ("PENDING", 0))
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM meta WHERE key='wechat_paused_at'").fetchone())
        counts = run(self.conn, sender=lambda text: "sent")
        self.assertEqual(counts["accepted"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications WHERE kind='batch_digest'").fetchone()[0], 1)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM meta WHERE key='wechat_paused_at'").fetchone())

    def test_config_validation(self):
        self.assertEqual(notify_config(self.conn, urgent_kinds=["verify_failed", " "])["urgent_kinds"], ["verify_failed"])
        with self.assertRaises(Exception):
            notify_config(self.conn, urgent_kinds=["batch_digest"])
        with self.assertRaises(Exception):
            notify_config(self.conn, digest_minutes=-1)


if __name__ == "__main__":
    unittest.main()
