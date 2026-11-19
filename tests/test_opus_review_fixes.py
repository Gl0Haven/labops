"""Regression tests for the Opus review of T20261019-labops-m0a (review/T20261019-labops-m0a.md)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops.config import now
from labops.db import connect, migrate, tx
from labops.errors import ValidationError, VersionConflict
from labops.gitio import tree_of
from labops.statemachine import transition
from labops.tasks import add_repo, create_task, create_worktree, open_round, snapshot, submit_changes
from tests.helpers import commit_file, git, make_repo, run_cli

EXECUTED_RESULTS = '[{"name":"unit","exit_code":0,"skipped":false}]'


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-opusfix-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        make_repo(self.repo, files={"calc.py": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
                                    "keep.txt": "base\n"})
        self.env = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.env.start()
        self.assertEqual(run_cli(self.home, "init", "--test-instance").returncode, 0)
        self.conn = connect(self.home / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo, worktree_root=self.root / "wt", trusted_for_exec=True)
        self.tid = "T20261019-opusfix"
        create_task(self.conn, repo_id="toy", task_id=self.tid, goal="fix", profile={"schema": 1, "commands": [], "allowed_paths": ["**"]})

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def round_with_worktree(self, actor="codex"):
        rnd = open_round(self.conn, task_id=self.tid, actor=actor, role="implement")
        return rnd, Path(create_worktree(self.conn, rnd["round_id"])["path"])

    def mark_verified(self, round_id, head):
        ph = self.conn.execute("SELECT profile_hash FROM tasks WHERE task_id=?", (self.tid,)).fetchone()[0]
        with tx(self.conn):
            transition(self.conn, "round", round_id, "VERIFYING", actor="test")
            transition(self.conn, "round", round_id, "VERIFIED", actor="test")
            self.conn.execute("INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,producer,created_at,finished_at,results_json) "
                              "VALUES(?,?,'INDEPENDENTLY_EXECUTED','passed',?,?,?,'test','t','t',?)",
                              (f"ev-{round_id}", round_id, head, tree_of(self.repo, head), ph, EXECUTED_RESULTS))


class SnapshotKeyTests(_Base):
    def test_second_snapshot_with_same_message_commits_new_edits(self):
        rnd, wt = self.round_with_worktree()
        (wt / "keep.txt").write_bytes(b"one\n")
        s1 = snapshot(self.conn, round_id=rnd["round_id"], files=["keep.txt"], message="wip", actor="codex")
        (wt / "keep.txt").write_bytes(b"two\n")
        s2 = snapshot(self.conn, round_id=rnd["round_id"], files=["keep.txt"], message="wip", actor="codex")
        self.assertNotEqual(s1["commit"], s2["commit"])
        self.assertFalse(s2["replayed"])
        self.assertEqual(git(wt, "status", "--porcelain").stdout.strip(), b"")

    def test_retry_after_success_replays(self):
        rnd, wt = self.round_with_worktree()
        (wt / "keep.txt").write_bytes(b"one\n")
        s1 = snapshot(self.conn, round_id=rnd["round_id"], files=["keep.txt"], message="wip", actor="codex")
        again = snapshot(self.conn, round_id=rnd["round_id"], files=["keep.txt"], message="wip", actor="codex")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["commit"], s1["commit"])


class PendingClaimTests(_Base):
    def test_received_claim_blocks_other_sha_until_reconcile(self):
        from labops.reconcile import reconcile
        rnd, wt = self.round_with_worktree()
        a = commit_file(wt, "keep.txt", "A\n")
        b = commit_file(wt, "keep.txt", "B\n")
        ref = f"refs/labops/{self.tid}/r1/codex/submitted"
        with tx(self.conn):
            self.conn.execute("INSERT INTO claims(claim_id,round_id,actor,kind,client_request_id,payload_json,payload_hash,fingerprint,status,created_at) "
                              "VALUES('cl_A',?,'codex','submit_changes','reqA','{}','x',?,'received',?)",
                              (rnd["round_id"], f"{self.tid}|1|codex|{a}", now()))
            self.conn.execute("INSERT INTO operations(op_id,repo_id,kind,ref,expected_old,target,status,idempotency_id,on_applied_json,created_at,updated_at) "
                              "VALUES('op_A','toy','create_ref',?,NULL,?,'PENDING','submit:cl_A',?,?,?)",
                              (ref, a, json.dumps({"type": "round_claimed", "round_id": rnd["round_id"], "commit": a,
                                                   "tree": tree_of(self.repo, a), "claim_id": "cl_A"}), now(), now()))
        with self.assertRaises(VersionConflict) as ctx:
            submit_changes(self.conn, round_id=rnd["round_id"], actor="codex", commit=b, client_request_id="reqB")
        self.assertEqual(str(ctx.exception), "round_claim_pending")
        reconcile(self.conn, "toy")
        row = self.conn.execute("SELECT state,head_commit FROM rounds WHERE round_id=?", (rnd["round_id"],)).fetchone()
        self.assertEqual((row["state"], row["head_commit"]), ("CLAIMED", a))
        self.assertEqual(self.conn.execute("SELECT status FROM claims WHERE claim_id='cl_A'").fetchone()[0], "accepted")

    def test_conflicting_pending_submit_rejects_claim(self):
        from labops.reconcile import reconcile
        rnd, wt = self.round_with_worktree()
        a = commit_file(wt, "keep.txt", "A\n")
        other = commit_file(wt, "keep.txt", "other\n")
        ref = f"refs/labops/{self.tid}/r1/codex/submitted"
        with tx(self.conn):
            self.conn.execute("INSERT INTO claims(claim_id,round_id,actor,kind,client_request_id,payload_json,payload_hash,fingerprint,status,created_at) "
                              "VALUES('cl_A',?,'codex','submit_changes','reqA','{}','x',?,'received',?)",
                              (rnd["round_id"], f"{self.tid}|1|codex|{a}", now()))
            self.conn.execute("INSERT INTO operations(op_id,repo_id,kind,ref,expected_old,target,status,idempotency_id,on_applied_json,created_at,updated_at) "
                              "VALUES('op_A','toy','create_ref',?,NULL,?,'PENDING','submit:cl_A',NULL,?,?)",
                              (ref, a, now(), now()))
        git(self.repo, "update-ref", ref, other)
        reconcile(self.conn, "toy")
        self.assertEqual(self.conn.execute("SELECT status FROM operations WHERE op_id='op_A'").fetchone()[0], "CONFLICT")
        self.assertEqual(self.conn.execute("SELECT status,reject_reason FROM claims WHERE claim_id='cl_A'").fetchone()[:],
                         ("rejected", "ref_conflict"))


class ApproveRecheckTests(_Base):
    def test_approve_after_new_round_goes_stale_and_main_unchanged(self):
        from labops.gate import approve_integration, gate_check, propose_integration
        from labops.review import open_review, submit_review
        rnd, wt = self.round_with_worktree()
        h1 = commit_file(wt, "keep.txt", "v1\n")
        submit_changes(self.conn, round_id=rnd["round_id"], actor="codex", commit=h1, client_request_id="s1")
        self.mark_verified(rnd["round_id"], h1)
        rv = open_review(self.conn, rnd["round_id"], "opus")
        submit_review(self.conn, review_id=rv["review_id"], actor="opus", verdict="PASS", findings=[], client_request_id="r1")
        self.assertTrue(gate_check(self.conn, self.tid)["pass"])
        ig = propose_integration(self.conn, self.tid)
        main_before = git(self.repo, "rev-parse", "refs/heads/main").stdout.strip()
        r2, wt2 = self.round_with_worktree()
        h2 = commit_file(wt2, "keep.txt", "v2\n")
        submit_changes(self.conn, round_id=r2["round_id"], actor="codex", commit=h2, client_request_id="s2")
        with patch.dict(os.environ, {"LABOPS_TEST_CONFIRM": ig["candidate_tree"][:12]}):
            result = approve_integration(self.conn, ig["integration_id"])
        self.assertEqual(result["state"], "STALE")
        self.assertEqual(result["reason"], "task_changed_since_proposal")
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/main").stdout.strip(), main_before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM approvals WHERE kind='integrate'").fetchone()[0], 0)


class RepairRefTests(_Base):
    def test_ref_deleted_twice_is_repaired_twice(self):
        from labops.reconcile import reconcile
        rnd, wt = self.round_with_worktree()
        h = commit_file(wt, "keep.txt", "x\n")
        submit_changes(self.conn, round_id=rnd["round_id"], actor="codex", commit=h, client_request_id="s")
        ref = f"refs/labops/{self.tid}/r1/codex/submitted"
        for _ in range(2):
            git(self.repo, "update-ref", "-d", ref)
            self.assertEqual(reconcile(self.conn, "toy")["refs_repaired"], 1)
            self.assertEqual(git(self.repo, "rev-parse", "--verify", "-q", ref).stdout.strip().decode(), h)
            self.assertTrue(all(v == 0 for v in reconcile(self.conn, "toy").values()))


class PolicyTrivialLineTests(_Base):
    def test_adding_function_to_symbol_frozen_file_is_not_a_hit(self):
        from labops.policy import frozen_hits
        base = git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()
        new = commit_file(self.repo, "calc.py", "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
                                                "\n\n# helper\ndef sub(a, b):\n    return a - b\n")
        policy = {"schema": 1, "frozen": [{"path": "calc.py", "symbols": ["add"], "ref": "s2", "note": ""}]}
        self.assertEqual(frozen_hits(self.repo, policy, base, new), [])

    def test_module_level_constant_still_blocks(self):
        from labops.policy import frozen_hits
        base = git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()
        new = commit_file(self.repo, "calc.py", "SCALE = 2\n\n\ndef add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
        policy = {"schema": 1, "frozen": [{"path": "calc.py", "symbols": ["add"], "ref": "s2", "note": ""}]}
        hits = frozen_hits(self.repo, policy, base, new)
        self.assertEqual([h["reason"] for h in hits], ["module_level"])


class TestInstanceTempOnlyTests(unittest.TestCase):
    def test_init_test_instance_outside_temp_is_refused(self):
        home = Path(__file__).resolve().parents[1] / ".opus-probe-home"
        try:
            result = run_cli(home, "init", "--test-instance")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((home / ".test-instance").exists())
        finally:
            import shutil
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
