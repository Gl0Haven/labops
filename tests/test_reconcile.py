import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops.db import connect, migrate, tx
from labops.gitio import read_ref, tree_of, update_ref_cas, zero_oid
from labops.reconcile import reconcile
from labops.statemachine import transition
from labops.tasks import add_repo, create_task, open_round, submit_changes
from tests.helpers import commit_file, git, make_repo, run_cli


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.head = make_repo(self.repo, files={"app.py": "VALUE = 1\n"})
        self.patch = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.patch.start()
        initialized = run_cli(self.home, "init", "--test-instance")
        self.assertEqual(initialized.returncode, 0, initialized.stdout.decode("utf-8", "replace"))
        self.conn = connect(self.home / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo, worktree_root=self.root / "worktrees")
        self.task_id = "T20261019-reconcile"
        create_task(self.conn, repo_id="toy", task_id=self.task_id, goal="reconcile", profile={"schema": 1})

    def tearDown(self):
        self.conn.close()
        self.patch.stop()
        self.temp.cleanup()

    def open_claimed(self, *, actor="codex", request="submit"):
        rnd = open_round(self.conn, task_id=self.task_id, actor=actor, role="implement")
        head = commit_file(self.repo, f"{rnd['n']}.txt", f"r{rnd['n']}\n")
        submit_changes(self.conn, round_id=rnd["round_id"], actor=actor, commit=head, client_request_id=request)
        return rnd, head

    def insert_op(self, *, kind, ref=None, expected=None, target=None, idem="manual", callback=None):
        op_id = "op-" + idem.replace(":", "-")
        self.conn.execute("INSERT INTO operations(op_id,repo_id,kind,ref,expected_old,target,status,idempotency_id,on_applied_json,created_at,updated_at) "
                          "VALUES(?,?,?,?,?,?,'PENDING',?,?,?,?)",
                          (op_id, "toy", kind, ref, expected, target or self.head, idem,
                           json.dumps(callback, sort_keys=True, separators=(",", ":")) if callback is not None else "null",
                           "2026-10-19T00:00:00Z", "2026-10-19T00:00:00Z"))
        return op_id

    def assert_second_reconcile_is_empty(self):
        result = reconcile(self.conn, "toy")
        self.assertEqual(result, {key: 0 for key in result})

    def test_pending_ref_applied_retried_and_conflicting(self):
        applied_ref = "refs/labops/manual/applied"
        applied_op = self.insert_op(kind="create_ref", ref=applied_ref, target=self.head, idem="applied")
        update_ref_cas(self.repo, applied_ref, self.head, zero_oid(self.repo))
        retry_ref = "refs/labops/manual/retry"
        retry_op = self.insert_op(kind="create_ref", ref=retry_ref, target=self.head, idem="retry")
        other = commit_file(self.repo, "other.txt", "other\n")
        conflict_ref = "refs/labops/manual/conflict"
        conflict_op = self.insert_op(kind="create_ref", ref=conflict_ref, target=self.head, idem="conflict")
        update_ref_cas(self.repo, conflict_ref, other, zero_oid(self.repo))
        result = reconcile(self.conn, "toy")
        self.assertEqual(result["operations_applied"], 2)
        self.assertEqual(result["operations_retried"], 1)
        self.assertEqual(result["operations_conflict"], 1)
        self.assertEqual(self.conn.execute("SELECT status FROM operations WHERE op_id=?", (applied_op,)).fetchone()[0], "APPLIED")
        self.assertEqual(self.conn.execute("SELECT status FROM operations WHERE op_id=?", (retry_op,)).fetchone()[0], "APPLIED")
        self.assertIn("retried", self.conn.execute("SELECT detail_json FROM operations WHERE op_id=?", (retry_op,)).fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT status FROM operations WHERE op_id=?", (conflict_op,)).fetchone()[0], "CONFLICT")
        self.assert_second_reconcile_is_empty()

    def test_pending_worktree_intent_is_retried(self):
        rnd = open_round(self.conn, task_id=self.task_id, actor="opus", role="fix")
        path = self.root / "worktrees" / "toy" / f"{self.task_id}-opus-r1"
        branch = f"labops/{self.task_id}/opus-r1"
        op = self.insert_op(kind="create_worktree", ref=f"refs/heads/{branch}", target=self.head,
                            idem=f"wt:{rnd['round_id']}", callback={"type": "worktree_recorded", "round_id": rnd["round_id"],
                                                                      "branch": branch, "path": str(path)})
        result = reconcile(self.conn, "toy")
        self.assertEqual(result["operations_applied"], 1)
        self.assertEqual(self.conn.execute("SELECT status FROM operations WHERE op_id=?", (op,)).fetchone()[0], "APPLIED")
        row = self.conn.execute("SELECT branch,worktree_path FROM rounds WHERE round_id=?", (rnd["round_id"],)).fetchone()
        self.assertEqual((row["branch"], Path(row["worktree_path"]).resolve()), (branch, path.resolve()))
        self.assertTrue(path.is_dir())
        self.assert_second_reconcile_is_empty()

    def test_verifying_round_is_interrupted_and_private_worktree_removed(self):
        rnd, head = self.open_claimed()
        verify_dir = self.home / "verify" / "ev-crash"
        git(self.repo, "worktree", "add", "--detach", str(verify_dir), head)
        profile_hash = self.conn.execute("SELECT profile_hash FROM tasks WHERE task_id=?", (self.task_id,)).fetchone()[0]
        with tx(self.conn):
            transition(self.conn, "round", rnd["round_id"], "VERIFYING", actor="test")
            self.conn.execute("INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,producer,created_at) "
                              "VALUES('ev-crash',?,'INDEPENDENTLY_EXECUTED','running',?,?,?,'labops','t')",
                              (rnd["round_id"], head, tree_of(self.repo, head), profile_hash))
        result = reconcile(self.conn, "toy")
        self.assertEqual(result["verification_interrupted"], 1)
        self.assertEqual(self.conn.execute("SELECT state FROM rounds WHERE round_id=?", (rnd["round_id"],)).fetchone()[0], "BLOCKED")
        self.assertEqual(self.conn.execute("SELECT status FROM evidence WHERE evidence_id='ev-crash'").fetchone()[0], "interrupted")
        self.assertFalse(verify_dir.exists())
        self.assert_second_reconcile_is_empty()

    def test_missing_submitted_ref_is_rebuilt_but_conflict_is_never_overwritten(self):
        rnd1, head1 = self.open_claimed(request="one")
        ref1 = f"refs/labops/{self.task_id}/r1/codex/submitted"
        git(self.repo, "update-ref", "-d", ref1, head1)
        first = reconcile(self.conn, "toy")
        self.assertEqual(first["refs_repaired"], 1)
        self.assertEqual(read_ref(self.repo, ref1), head1)
        self.assert_second_reconcile_is_empty()
        # A different round lets the test introduce an integrity conflict independently.
        with tx(self.conn):
            transition(self.conn, "round", rnd1["round_id"], "VERIFYING", actor="test")
            transition(self.conn, "round", rnd1["round_id"], "BLOCKED", actor="test")
        rnd2 = open_round(self.conn, task_id=self.task_id, actor="opus", role="fix")
        other = commit_file(self.repo, "different.txt", "different\n")
        submit_changes(self.conn, round_id=rnd2["round_id"], actor="opus", commit=other, client_request_id="two")
        ref2 = f"refs/labops/{self.task_id}/r2/opus/submitted"
        update_ref_cas(self.repo, ref2, head1, other)
        before = read_ref(self.repo, ref2)
        conflict = reconcile(self.conn, "toy")
        self.assertEqual(conflict["integrity_conflicts"], 1)
        self.assertEqual(read_ref(self.repo, ref2), before)
        events = self.conn.execute("SELECT COUNT(*) FROM events WHERE entity_type='round' AND to_state='integrity_conflict'").fetchone()[0]
        self.assertEqual(events, 1)
        self.assert_second_reconcile_is_empty()

    def test_main_advanced_integration_goes_stale_and_unknown_ref_is_recorded_once(self):
        rnd, head = self.open_claimed()
        # Integration freshness is independent of approval state; create a representative pending candidate.
        self.conn.execute("INSERT INTO integrations(integration_id,task_id,candidate_commit,candidate_tree,main_before,state,created_at,updated_at) "
                          "VALUES('ig-stale',?,?,?,?,'AWAITING_HUMAN_APPROVAL','t','t')",
                          (self.task_id, head, tree_of(self.repo, head), self.head))
        advance = commit_file(self.repo, "main-advance.txt", "advance\n")
        unknown_ref = "refs/labops/unknown/unbound"
        update_ref_cas(self.repo, unknown_ref, advance, zero_oid(self.repo))
        first = reconcile(self.conn, "toy")
        self.assertEqual(first["integrations_stale"], 1)
        self.assertEqual(first["unknown_refs"], 1)
        self.assertEqual(self.conn.execute("SELECT state FROM integrations WHERE integration_id='ig-stale'").fetchone()[0], "STALE")
        self.assertEqual(read_ref(self.repo, unknown_ref), advance)
        self.assert_second_reconcile_is_empty()


if __name__ == "__main__":
    unittest.main()
