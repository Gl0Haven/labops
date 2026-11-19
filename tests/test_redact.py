import json
import tempfile
import unittest
from pathlib import Path

from labops.db import connect, migrate
from labops.context import task_context
from labops.redact import confidential_repo_ids, for_actor
from labops.tasks import add_repo, create_task, set_confidential, set_task_brief
from tests.helpers import make_repo


class RedactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-redact-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        make_repo(self.repo)
        self.conn = connect(self.root / "home" / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="private", path=self.repo)
        create_task(self.conn, repo_id="private", task_id="T20261020-private", goal="PRIVATE GOAL",
                    profile={"schema": 1, "commands": [{"name": "check", "argv": ["python", "C:/private/run.py"],
                                                           "timeout_s": 30}]})
        set_confidential(self.conn, "private", True)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_confidential_payload_removes_sensitive_fields_and_shortens_hashes(self):
        full_sha = "a" * 40
        payload = {
            "task_id": "T20261020-private",
            "goal": "PRIVATE GOAL",
            "current": {"n": 2, "state": "OPEN", "worktree_path": "C:/private/worktree",
                        "branch": "private/branch", "head_commit": full_sha,
                        "submitted_ref": {"name": "refs/labops/T20261020-private/r2/codex/submitted"}},
            "open_findings": {"findings": [{"text": "PRIVATE FINDING", "path": "src/private.py"}]},
            "acceptance": [{"argv": ["python", "C:/private/run.py"]}],
            "policy": {"frozen": [{"path": "src/private.py", "ref": "D-20261020-1"}]},
            "refs": {"handoff": "C:/Users/private/handoff.json"},
            "base_commit": full_sha,
        }
        result = for_actor(self.conn, "secretary", payload)
        encoded = json.dumps(result, ensure_ascii=False)
        for secret in ("PRIVATE GOAL", "PRIVATE FINDING", "C:/private", "src/private.py", "C:/Users/private",
                       "private/branch", "refs/labops"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(result["current"]["head_commit"], "a" * 12)
        self.assertEqual(result["base_commit"], "a" * 12)
        self.assertEqual(result["current"]["n"], 2)

    def test_progress_summary_redacts_only_confidential_task_rows(self):
        payload = {"tasks": [
            {"task_id": "T20261020-private", "repo_id": "private", "goal": "PRIVATE GOAL",
             "latest_round": {"n": 1, "head": "b" * 40}},
            {"task_id": "T20261020-public", "repo_id": "public", "goal": "PUBLIC GOAL"},
        ]}
        result = for_actor(self.conn, "secretary", payload)
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("PRIVATE GOAL", encoded)
        self.assertNotIn('"repo_id": "private"', encoded)
        self.assertIn("PUBLIC GOAL", encoded)
        self.assertIn('"repo_id": "public"', encoded)
        self.assertEqual(result["tasks"][0]["latest_round"]["head"], "b" * 12)

    def test_other_actors_and_nonconfidential_tasks_are_unchanged(self):
        payload = {"task_id": "T20261020-private", "goal": "PRIVATE GOAL", "base_commit": "c" * 40}
        self.assertEqual(for_actor(self.conn, "opus", payload), payload)
        self.assertEqual(confidential_repo_ids(self.conn), {"private"})

    def test_confidential_context_keeps_generic_rules_but_removes_task_brief(self):
        set_task_brief(self.conn, "T20261020-private", "PRIVATE HANDOFF BRIEF")
        context = task_context(self.conn, task_id="T20261020-private")
        result = for_actor(self.conn, "secretary", context)
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("PRIVATE HANDOFF BRIEF", encoded)
        self.assertIn("LabOps", result["sections"]["handoff"]["rules"])
        self.assertNotIn("brief", result["sections"]["handoff"])


if __name__ == "__main__":
    unittest.main()
