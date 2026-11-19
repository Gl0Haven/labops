import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops.approvals import approve_frozen, approve_self_review
from labops.db import connect, migrate, tx
from labops.gitio import tree_of, run as git_run
from labops.gate import evaluate, gate_check
from labops.policy import frozen_hits
from labops.review import open_review, submit_review
from labops.statemachine import transition
from labops.tasks import add_repo, create_task, open_round, set_policy, submit_changes
from tests.helpers import commit_file, make_repo, run_cli

EXECUTED_RESULTS = '[{"name":"unit","exit_code":0,"skipped":false}]'


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        make_repo(self.repo, files={"calc.py": "def add(a,b):\n    return a+b\n", "keep.txt": "base\n"})
        self.env = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.env.start()
        initialized = run_cli(self.home, "init", "--test-instance")
        self.assertEqual(initialized.returncode, 0, initialized.stdout.decode("utf-8", "replace"))
        self.conn = connect(self.home / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo, worktree_root=self.root / "worktrees", trusted_for_exec=True)
        self.task_id = "T20261019-gate"
        self.profile = {"schema": 1, "commands": [], "allowed_paths": ["**"]}

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def prepare_round(self, *, actor="codex", change_path="keep.txt", content="changed\n", policy=None,
                      reviewer="opus", verdict="PASS", add_review=True):
        if policy is not None:
            set_policy(self.conn, "toy", policy)
        create_task(self.conn, repo_id="toy", task_id=self.task_id, goal="gate", profile=self.profile)
        rnd = open_round(self.conn, task_id=self.task_id, actor=actor, role="fix" if actor == "opus" else "implement")
        from labops.tasks import create_worktree
        wt = Path(create_worktree(self.conn, rnd["round_id"])["path"])
        head = commit_file(wt, change_path, content)
        submit_changes(self.conn, round_id=rnd["round_id"], actor=actor, commit=head, client_request_id="submit")
        profile_hash = self.conn.execute("SELECT profile_hash FROM tasks WHERE task_id=?", (self.task_id,)).fetchone()[0]
        with tx(self.conn):
            transition(self.conn, "round", rnd["round_id"], "VERIFYING", actor="test")
            transition(self.conn, "round", rnd["round_id"], "VERIFIED", actor="test")
            self.conn.execute("INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,producer,created_at,finished_at,results_json) "
                              "VALUES('ev-test',?,'INDEPENDENTLY_EXECUTED','passed',?,?,?,'test','t','t',?)",
                              (rnd["round_id"], head, tree_of(self.repo, head), profile_hash, EXECUTED_RESULTS))
        review = None
        if add_review:
            review = open_review(self.conn, rnd["round_id"], reviewer)
            submit_review(self.conn, review_id=review["review_id"], actor=reviewer, verdict=verdict,
                          findings=[], client_request_id="review")
        return rnd, head, review

    def test_z2_independent_evidence_condition(self):
        self.prepare_round()
        self.conn.execute("DELETE FROM evidence")
        result = gate_check(self.conn, self.task_id)
        self.assertFalse(result["pass"])
        self.assertIn("no_independent_evidence", result["reasons"])

    def test_z2_passed_evidence_without_executed_checks_is_rejected(self):
        # GPT review P0: a passed row with no executed command (e.g. written before the verify fix)
        # must not satisfy the gate.
        self.prepare_round()
        for results in ("[]", '[{"name":"unit","exit_code":null,"skipped":true}]', "not json"):
            with tx(self.conn):
                self.conn.execute("UPDATE evidence SET results_json=?", (results,))
            result = gate_check(self.conn, self.task_id)
            self.assertFalse(result["pass"], results)
            self.assertIn("evidence_without_checks", result["reasons"])

    def test_z2_pass_review_condition(self):
        self.prepare_round(add_review=False)
        result = gate_check(self.conn, self.task_id)
        self.assertIn("no_pass_review", result["reasons"])

    def test_z2_frozen_change_requires_sha_bound_decision(self):
        policy = {"schema": 1, "frozen": [{"path": "calc.py", "symbols": ["add"], "ref": "§3.3", "note": "frozen add"}]}
        self.prepare_round(change_path="calc.py", content="def add(a,b):\n    return a+b+1\n", policy=policy)
        blocked = gate_check(self.conn, self.task_id)
        self.assertIn("frozen_zone_touched", blocked["reasons"])
        self.assertEqual(blocked["detail"]["limits"], "AST mapping is not a completeness proof")
        set_policy(self.conn, "toy", {"schema": 1, "frozen": []})
        self.assertIn("frozen_zone_touched", gate_check(self.conn, self.task_id)["reasons"])
        with patch.dict(os.environ, {"LABOPS_TEST_CONFIRM": "D-20261019-9"}):
            approval = approve_frozen(self.conn, self.task_id, "D-20261019-9")
        self.assertEqual(approval["target_commit"], blocked["detail"]["head_commit"])
        passed = gate_check(self.conn, self.task_id)
        self.assertTrue(passed["pass"])

    def test_z2_blocking_review_condition(self):
        self.prepare_round(verdict="REWORK_REQUIRED")
        result = gate_check(self.conn, self.task_id)
        self.assertIn("open_blocking_review", result["reasons"])

    def test_z2_submitted_ref_condition(self):
        rnd, head, _ = self.prepare_round()
        ref = f"refs/labops/{self.task_id}/r1/codex/submitted"
        git_run(self.repo, "update-ref", "-d", ref, head, read_only=False)
        result = gate_check(self.conn, self.task_id)
        self.assertIn("submitted_ref_mismatch", result["reasons"])

    def test_z1_self_review_needs_and_accepts_user_approval(self):
        rnd, head, _ = self.prepare_round(actor="opus", reviewer="opus")
        result = gate_check(self.conn, self.task_id)
        self.assertIn("self_review_needs_acceptance", result["reasons"])
        tree = tree_of(self.repo, head)
        with patch.dict(os.environ, {"LABOPS_TEST_CONFIRM": tree[:12]}):
            approval = approve_self_review(self.conn, self.task_id)
        self.assertEqual(approval["target_tree"], tree)
        self.assertTrue(gate_check(self.conn, self.task_id)["pass"])

    def test_evaluate_is_read_only_and_gate_check_preserves_transition(self):
        self.prepare_round()
        before_events = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        evaluated = evaluate(self.conn, self.task_id)
        self.assertTrue(evaluated["pass"], evaluated)
        self.assertEqual(self.conn.execute("SELECT state FROM tasks WHERE task_id=?", (self.task_id,)).fetchone()[0], "ACTIVE")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], before_events)

        checked = gate_check(self.conn, self.task_id)
        self.assertEqual(checked, evaluated)
        self.assertEqual(self.conn.execute("SELECT state FROM tasks WHERE task_id=?", (self.task_id,)).fetchone()[0], "READY_FOR_INTEGRATION")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], before_events + 1)


if __name__ == "__main__":
    unittest.main()
