import tempfile
import unittest
from pathlib import Path

from labops.config import now
from labops.db import connect, migrate
from labops.errors import ValidationError
from labops.progress import progress_summary
from labops.redact import for_actor
from labops.tasks import add_repo, create_task, open_round
from tests.helpers import make_repo


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-progress-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.base = make_repo(self.repo)
        self.conn = connect(self.root / "home" / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo)
        create_task(self.conn, repo_id="toy", task_id="T20261020-active", goal="active goal", profile={"schema": 1})
        self.round = open_round(self.conn, task_id="T20261020-active", actor="codex", role="implement")

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_summary_includes_active_task_recent_events_and_approval_command(self):
        timestamp = now()
        self.conn.execute(
            "INSERT INTO integrations(integration_id,task_id,candidate_commit,candidate_tree,main_before,state,check_json,created_at,updated_at) "
            "VALUES('ig-progress',? ,?,?,?,'AWAITING_HUMAN_APPROVAL','{}',?,?)",
            ("T20261020-active", self.base, "tree", self.base, timestamp, timestamp),
        )
        before_events = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        summary = progress_summary(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], before_events)
        self.assertEqual(summary["counts"]["active"], 1)
        self.assertEqual(summary["counts"]["awaiting_approval"], 1)
        self.assertEqual(summary["tasks"][0]["task_id"], "T20261020-active")
        self.assertEqual(summary["tasks"][0]["latest_round"], {
            "n": 1, "actor": "codex", "state": "OPEN", "head": None,
        })
        self.assertEqual(summary["tasks"][0]["next_action"], "implement_then_snapshot_and_submit")
        self.assertIn({
            "task_id": "T20261020-active",
            "kind": "integration_approval",
            "command": "labops integrate approve --integration ig-progress",
        }, summary["pending_user_actions"])
        self.assertTrue(summary["recent_events"])
        self.assertEqual(summary["snapshot_version"], max(event["seq"] for event in summary["recent_events"]))

    def test_window_validation(self):
        for invalid in (0, 169, True, 24.0, "24"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                progress_summary(self.conn, since_hours=invalid)

    def test_alert_pause_is_exposed_to_secretary(self):
        self.conn.execute("INSERT INTO meta(key,value) VALUES('wechat_paused_at','2026-10-20T12:00:00Z')")
        summary = for_actor(self.conn, "secretary", progress_summary(self.conn))
        self.assertEqual(summary["alerts_paused"], {
            "since": "2026-10-20T12:00:00Z", "hint": "微信凭证失效：请给 bot 发一句话",
        })


if __name__ == "__main__":
    unittest.main()
