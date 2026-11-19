import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops.db import connect, migrate
from labops.errors import Blocked, GitConflict, IdempotencyConflict, ValidationError, VersionConflict
from labops.gitio import update_ref_cas, zero_oid
from labops.tasks import add_repo, create_task, create_worktree, open_round, submit_changes
from tests.helpers import commit_file, git, make_repo


class TasksSubmitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "toy"
        self.initial = make_repo(self.repo, files={"calc.py": "def add(a, b):\n    return a + b\n"})
        self.env_patch = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.env_patch.start()
        self.conn = connect(self.home / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo, worktree_root=self.root / "worktrees")
        self.task_id = "T20261019-submit"
        create_task(self.conn, repo_id="toy", task_id=self.task_id, goal="test submit", profile={"schema": 1})
        self.round = open_round(self.conn, task_id=self.task_id, actor="codex", role="implement")

    def tearDown(self):
        self.conn.close()
        self.env_patch.stop()
        self.temp.cleanup()

    def new_commit(self, name="result.txt", text="result\n", message="result"):
        return commit_file(self.repo, name, text, message)

    def test_valid_submit_binds_round_and_replay_is_idempotent(self):
        head = self.new_commit()
        result = submit_changes(self.conn, round_id=self.round["round_id"], actor="codex", commit=head, client_request_id="req-1")
        self.assertEqual(result["status"], "accepted")
        replay = submit_changes(self.conn, round_id=self.round["round_id"], actor="codex", commit=head, client_request_id="req-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["claim_id"], result["claim_id"])
        row = self.conn.execute("SELECT state,head_commit,claim_id FROM rounds WHERE round_id=?", (self.round["round_id"],)).fetchone()
        self.assertEqual((row["state"], row["head_commit"], row["claim_id"]), ("CLAIMED", head, result["claim_id"]))
        self.assertEqual(self.conn.execute("SELECT status FROM claims WHERE claim_id=?", (result["claim_id"],)).fetchone()[0], "accepted")

    def test_request_id_payload_conflict(self):
        head = self.new_commit()
        submit_changes(self.conn, round_id=self.round["round_id"], actor="codex", commit=head, client_request_id="same", notes={"x": 1})
        with self.assertRaises(IdempotencyConflict):
            submit_changes(self.conn, round_id=self.round["round_id"], actor="codex", commit=head, client_request_id="same", notes={"x": 2})

    def test_duplicate_fingerprint_and_second_sha_conflict(self):
        first = self.new_commit("one.txt", "one\n", "one")
        accepted = submit_changes(self.conn, round_id=self.round["round_id"], actor="codex", commit=first, client_request_id="one")
        duplicate = submit_changes(self.conn, round_id=self.round["round_id"], actor="codex", commit=first, client_request_id="two")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["duplicate_of"], accepted["claim_id"])
        second = self.new_commit("two.txt", "two\n", "two")
        with self.assertRaises(VersionConflict):
            submit_changes(self.conn, round_id=self.round["round_id"], actor="codex", commit=second, client_request_id="three")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM claims WHERE status='accepted'").fetchone()[0], 1)

    def test_rejected_inputs_are_recorded_and_round_stays_open(self):
        with self.assertRaises(ValidationError):
            submit_changes(self.conn, round_id=self.round["round_id"], actor="opus", commit=self.initial, client_request_id="actor")
        with self.assertRaises(ValidationError):
            submit_changes(self.conn, round_id=self.round["round_id"], actor="codex", commit="missing", client_request_id="missing")
        with self.assertRaises(ValidationError):
            submit_changes(self.conn, round_id=self.round["round_id"], actor="codex", commit=self.initial, client_request_id="empty")
        reasons = {r[0] for r in self.conn.execute("SELECT reject_reason FROM claims WHERE status='rejected'")}
        self.assertEqual(reasons, {"commit_not_found", "empty_round"})
        self.assertEqual(self.conn.execute("SELECT state FROM rounds WHERE round_id=?", (self.round["round_id"],)).fetchone()[0], "OPEN")

    def test_non_descendant_is_rejected(self):
        unrelated = self.root / "unrelated"
        unrelated_head = make_repo(unrelated, files={"orphan.txt": "orphan\n"})
        git(self.repo, "fetch", str(unrelated), unrelated_head)
        with self.assertRaisesRegex(ValidationError, "not_descendant_of_base"):
            submit_changes(self.conn, round_id=self.round["round_id"], actor="codex", commit=unrelated_head, client_request_id="orphan")
        row = self.conn.execute("SELECT reject_reason FROM claims WHERE client_request_id='orphan'").fetchone()
        self.assertEqual(row[0], "not_descendant_of_base")

    def test_round_open_and_worktree_boundaries(self):
        with self.assertRaises(Blocked):
            open_round(self.conn, task_id=self.task_id, actor="opus", role="fix")
        created = create_worktree(self.conn, self.round["round_id"])
        self.assertEqual(created["status"], "APPLIED")
        row = self.conn.execute("SELECT branch,worktree_path FROM rounds WHERE round_id=?", (self.round["round_id"],)).fetchone()
        self.assertEqual(row["branch"], created["branch"])
        self.assertEqual(Path(row["worktree_path"]).resolve(), Path(created["path"]).resolve())
        head = self.new_commit("r1.txt", "r1\n")
        submit_changes(self.conn, round_id=self.round["round_id"], actor="codex", commit=head, client_request_id="r1")
        self.conn.execute("UPDATE rounds SET state='VERIFIED' WHERE round_id=?", (self.round["round_id"],))
        second = open_round(self.conn, task_id=self.task_id, actor="opus", role="fix")
        self.assertEqual(second["base_commit"], head)
        expected_path = self.root / "worktrees" / "toy" / f"{self.task_id}-opus-r2"
        expected_path.mkdir(parents=True)
        (expected_path / "keep.txt").write_text("preserve", encoding="utf-8")
        result = create_worktree(self.conn, second["round_id"])
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual((expected_path / "keep.txt").read_text(encoding="utf-8"), "preserve")

    def test_existing_branch_without_worktree_conflicts(self):
        second_task = "T20261019-branch"
        create_task(self.conn, repo_id="toy", task_id=second_task, goal="branch test", profile={"schema": 1})
        rnd = open_round(self.conn, task_id=second_task, actor="codex", role="implement")
        branch = f"refs/heads/labops/{second_task}/codex-r1"
        self.assertTrue(update_ref_cas(self.repo, branch, self.initial, zero_oid(self.repo)))
        self.assertEqual(create_worktree(self.conn, rnd["round_id"])["status"], "CONFLICT")


if __name__ == "__main__":
    unittest.main()
