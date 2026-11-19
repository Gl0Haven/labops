import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops.db import connect, migrate
from labops.errors import Blocked
from labops.gate import approve_integration, gate_check, propose_integration
from labops.gitio import read_ref
from labops.review import open_review, submit_review
from labops.tasks import add_repo, create_task, create_worktree, open_round, submit_changes
from tests.helpers import commit_file, make_repo, run_cli

EXECUTED_RESULTS = '[{"name":"unit","exit_code":0,"skipped":false}]'


class IntegrateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.source = self.root / "source"
        make_repo(self.source, files={"app.py": "VALUE = 1\n"})
        self.repo = self.source
        self.patch = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.patch.start()
        initialized = run_cli(self.home, "init", "--test-instance")
        self.assertEqual(initialized.returncode, 0, initialized.stdout.decode("utf-8", "replace"))
        self.conn = connect(self.home / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo, worktree_root=self.root / "worktrees", trusted_for_exec=True)
        self.task_id = "T20261019-integrate"
        create_task(self.conn, repo_id="toy", task_id=self.task_id, goal="integrate", profile={"schema": 1, "allowed_paths": ["**"]})
        rnd = open_round(self.conn, task_id=self.task_id, actor="codex", role="implement")
        wt = Path(create_worktree(self.conn, rnd["round_id"])["path"])
        self.candidate = commit_file(wt, "app.py", "VALUE = 2\n")
        submit_changes(self.conn, round_id=rnd["round_id"], actor="codex", commit=self.candidate, client_request_id="submit")
        profile_hash = self.conn.execute("SELECT profile_hash FROM tasks WHERE task_id=?", (self.task_id,)).fetchone()[0]
        from labops.db import tx
        from labops.statemachine import transition
        from labops.gitio import tree_of
        with tx(self.conn):
            transition(self.conn, "round", rnd["round_id"], "VERIFYING", actor="test")
            transition(self.conn, "round", rnd["round_id"], "VERIFIED", actor="test")
            self.conn.execute("INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,producer,created_at,finished_at,results_json) "
                              "VALUES('ev-test',?,'INDEPENDENTLY_EXECUTED','passed',?,?,?,'test','t','t',?)",
                              (rnd["round_id"], self.candidate, tree_of(self.repo, self.candidate), profile_hash,
                               EXECUTED_RESULTS))
        review = open_review(self.conn, rnd["round_id"], "opus")
        submit_review(self.conn, review_id=review["review_id"], actor="opus", verdict="PASS", findings=[], client_request_id="review")
        self.assertTrue(gate_check(self.conn, self.task_id)["pass"])
        self.main_before = read_ref(self.repo, "refs/heads/main")
        self.integration = propose_integration(self.conn, self.task_id)

    def tearDown(self):
        self.conn.close()
        self.patch.stop()
        self.temp.cleanup()

    def test_fast_forward_main_worktree_and_consumes_approvals(self):
        self.assertEqual(self.integration["state"], "AWAITING_HUMAN_APPROVAL")
        with patch.dict(os.environ, {"LABOPS_TEST_CONFIRM": self.integration["candidate_tree"][:12]}):
            result = approve_integration(self.conn, self.integration["integration_id"])
        self.assertEqual(result["state"], "MERGED")
        self.assertFalse(result["client_restart_required"])
        self.assertEqual(result["reason"], "no_mcp_tool_surface_change")
        self.assertEqual(read_ref(self.repo, "refs/heads/main"), self.candidate)
        self.assertEqual(self.conn.execute("SELECT state FROM tasks WHERE task_id=?", (self.task_id,)).fetchone()[0], "INTEGRATED")
        self.assertIsNotNone(self.conn.execute("SELECT consumed_at FROM approvals WHERE integration_id=?", (self.integration["integration_id"],)).fetchone()[0])

    def test_restart_requirement_is_returned_from_successful_approval(self):
        with patch("labops.gate.client_restart_requirement",
                   return_value={"client_restart_required": True, "reason": "mcp_server_changed"}):
            with patch.dict(os.environ, {"LABOPS_TEST_CONFIRM": self.integration["candidate_tree"][:12]}):
                result = approve_integration(self.conn, self.integration["integration_id"])
        self.assertEqual(result["state"], "MERGED")
        self.assertTrue(result["client_restart_required"])
        self.assertEqual(result["reason"], "mcp_server_changed")

    def test_dirty_main_worktree_blocks_merge(self):
        (self.repo / "app.py").write_text("VALUE = 'dirty'\n", encoding="utf-8")
        with patch.dict(os.environ, {"LABOPS_TEST_CONFIRM": self.integration["candidate_tree"][:12]}):
            with self.assertRaisesRegex(Blocked, "main_worktree_dirty"):
                approve_integration(self.conn, self.integration["integration_id"])
        self.assertEqual(read_ref(self.repo, "refs/heads/main"), self.main_before)
        self.assertEqual(self.conn.execute("SELECT state FROM integrations WHERE integration_id=?", (self.integration["integration_id"],)).fetchone()[0], "AWAITING_HUMAN_APPROVAL")

    def test_main_advanced_before_approval_marks_integration_stale(self):
        advanced = commit_file(self.repo, "main-only.txt", "advanced\n")
        with patch.dict(os.environ, {"LABOPS_TEST_CONFIRM": self.integration["candidate_tree"][:12]}):
            result = approve_integration(self.conn, self.integration["integration_id"])
        self.assertEqual((result["state"], result["reason"]), ("STALE", "main_advanced"))
        self.assertEqual(read_ref(self.repo, "refs/heads/main"), advanced)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM approvals WHERE integration_id=?", (self.integration["integration_id"],)).fetchone()[0], 0)

    def test_bare_repo_without_main_worktree_uses_ref_cas(self):
        # A bare copy leaves main unchecked out, so integration must take the CAS path.
        bare = self.root / "bare.git"
        subprocess.run(["git", "clone", "--bare", str(self.source), str(bare)], check=True, capture_output=True)
        conn = connect(self.home / "bare.db")
        migrate(conn)
        add_repo(conn, repo_id="bare", path=bare, worktree_root=self.root / "bare-worktrees", trusted_for_exec=True)
        task_id = "T20261019-bare"
        create_task(conn, repo_id="bare", task_id=task_id, goal="bare CAS", profile={"schema": 1, "allowed_paths": ["**"]})
        rnd = open_round(conn, task_id=task_id, actor="codex", role="implement")
        wt = Path(create_worktree(conn, rnd["round_id"])["path"])
        candidate = commit_file(wt, "bare.txt", "candidate\n")
        submit_changes(conn, round_id=rnd["round_id"], actor="codex", commit=candidate, client_request_id="submit")
        profile_hash = conn.execute("SELECT profile_hash FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
        from labops.db import tx
        from labops.statemachine import transition
        from labops.gitio import tree_of
        with tx(conn):
            transition(conn, "round", rnd["round_id"], "VERIFYING", actor="test")
            transition(conn, "round", rnd["round_id"], "VERIFIED", actor="test")
            conn.execute("INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,producer,created_at,finished_at,results_json) VALUES('ev-bare',?,'INDEPENDENTLY_EXECUTED','passed',?,?,?,'test','t','t',?)",
                         (rnd["round_id"], candidate, tree_of(bare, candidate), profile_hash, EXECUTED_RESULTS))
        review = open_review(conn, rnd["round_id"], "opus")
        submit_review(conn, review_id=review["review_id"], actor="opus", verdict="PASS", findings=[], client_request_id="review")
        self.assertTrue(gate_check(conn, task_id)["pass"])
        integration = propose_integration(conn, task_id)
        with patch.dict(os.environ, {"LABOPS_TEST_CONFIRM": integration["candidate_tree"][:12]}):
            result = approve_integration(conn, integration["integration_id"])
        self.assertEqual(result["state"], "MERGED")
        self.assertEqual(read_ref(bare, "refs/heads/main"), candidate)
        conn.close()


if __name__ == "__main__":
    unittest.main()
