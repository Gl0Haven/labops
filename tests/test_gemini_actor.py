import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops import gpuq, ids, next_task, notify
from labops.db import connect, migrate
from labops.tasks import add_repo, create_task, open_round, submit_changes
from labops.verify import verify_round
from tests.helpers import commit_file, make_repo

RUNNABLE_PROFILE = {
    "schema": 1,
    "commands": [{"name": "unit", "argv": ["{python}", "-c", "pass"], "cwd": ".", "timeout_s": 60}],
}
MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None
if MCP_AVAILABLE:
    from mcp.server.mcpserver.exceptions import ToolError
    from labops import mcp_server
else:
    ToolError = mcp_server = None


class GeminiActorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-gemini-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.base = make_repo(self.repo)
        self.home = self.root / "home"
        self.db_path = self.home / "labops.db"
        self.env_patch = patch.dict(os.environ, {"LABOPS_HOME": str(self.home), "LABOPS_ACTOR": "gemini"})
        self.env_patch.start()
        conn = connect(self.db_path)
        try:
            migrate(conn)
            add_repo(conn, repo_id="toy", path=self.repo, worktree_root=self.root / "wt", trusted_for_exec=True)
        finally:
            conn.close()

    def tearDown(self):
        self.env_patch.stop()
        self.temp.cleanup()

    def test_ids_and_notify_and_gpuq_accept_gemini(self):
        ids.validate_actor("gemini")
        conn = connect(self.db_path)
        try:
            res = notify.notify_user(conn, actor="gemini", text="status ok", request_id="gemini-req-1")
            self.assertTrue(res["queued"])
            row = conn.execute("SELECT text FROM notifications WHERE event_key=?", (res["event_key"],)).fetchone()
            self.assertIn("[LabOps · Gemini 回报]", row["text"])

            intent = gpuq.queue_intent(conn, repo_id="toy", title="run eval", note="after build", created_by="gemini")
            self.assertEqual(intent["queue_item"]["created_by"], "gemini")
        finally:
            conn.close()

    def test_next_task_gemini_full_workflow(self):
        conn = connect(self.db_path)
        try:
            self.assertEqual(next_task.next_task(conn, "gemini"), {"action": "idle"})
            create_task(conn, repo_id="toy", task_id="T20261120-g1", goal="Test gemini", profile=RUNNABLE_PROFILE, base=self.base)
            step = next_task.next_task(conn, "gemini")
            self.assertEqual(step["action"], "start_new")

            open_round(conn, task_id="T20261120-g1", actor="codex", role="implement")
            sha = commit_file(self.repo, "a.txt", "hello\n")
            submit_changes(conn, round_id="T20261120-g1/r1", actor="codex", commit=sha, client_request_id="g1-c1")
            step = next_task.next_task(conn, "gemini")
            self.assertEqual(step["action"], "verify")
            verify_round(conn, round_id="T20261120-g1/r1")
            step = next_task.next_task(conn, "gemini")
            self.assertEqual(step["action"], "review")
        finally:
            conn.close()

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_mcp_gemini_allows_implement_and_fix(self):
        conn = connect(self.db_path)
        try:
            create_task(conn, repo_id="toy", task_id="T20261120-g2", goal="MCP gemini", profile=RUNNABLE_PROFILE, base=self.base)
            create_task(conn, repo_id="toy", task_id="T20261120-g3", goal="MCP gemini fix", profile=RUNNABLE_PROFILE, base=self.base)
        finally:
            conn.close()

        r_impl = mcp_server.start_work(task_id="T20261120-g2", role="implement")
        self.assertEqual(r_impl["round_id"], "T20261120-g2/r1")
        r_fix = mcp_server.start_work(task_id="T20261120-g3", role="fix")
        self.assertEqual(r_fix["round_id"], "T20261120-g3/r1")

        summary = mcp_server.progress_summary(since_hours=24)
        self.assertIn("tasks", summary)

        with self.assertRaises(ToolError) as denied:
            mcp_server.desktop_dispatch(kind="codex", target="x", prompt="hi", request_id="req-12345678")
        self.assertEqual(json.loads(str(denied.exception))["message"], "actor_tool_not_allowed")


if __name__ == "__main__":
    unittest.main()
