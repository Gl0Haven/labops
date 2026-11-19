import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops.db import connect, migrate, tx
from labops.errors import Blocked, IdempotencyConflict, ValidationError
from labops.gitio import tree_of
from labops.review import open_review, submit_review
from labops.statemachine import transition
from labops.tasks import add_repo, create_task, open_round, submit_changes
from tests.helpers import commit_file, make_repo


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        make_repo(self.repo, files={"app.py": "VALUE = 1\n"})
        self.patch = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.patch.start()
        self.conn = connect(self.home / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo)
        self.task_id = "T20261019-review"
        create_task(self.conn, repo_id="toy", task_id=self.task_id, goal="review", profile={"schema": 1})

    def tearDown(self):
        self.conn.close()
        self.patch.stop()
        self.temp.cleanup()

    def verified_round(self, actor="codex", *, filename="app.py", content="VALUE = 2\n"):
        rnd = open_round(self.conn, task_id=self.task_id, actor=actor, role="fix" if actor == "opus" else "implement")
        head = commit_file(self.repo, filename, content)
        submit_changes(self.conn, round_id=rnd["round_id"], actor=actor, commit=head, client_request_id=f"{rnd['n']}-submit")
        with tx(self.conn):
            transition(self.conn, "round", rnd["round_id"], "VERIFYING", actor="test")
            transition(self.conn, "round", rnd["round_id"], "VERIFIED", actor="test")
            profile_hash = self.conn.execute("SELECT profile_hash FROM tasks WHERE task_id=?", (self.task_id,)).fetchone()[0]
            self.conn.execute("INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,producer,created_at,finished_at) "
                              "VALUES(?,?,'INDEPENDENTLY_EXECUTED','passed',?,?,?,'test','t','t')",
                              (f"ev-{rnd['n']}", rnd["round_id"], head, tree_of(self.repo, head), profile_hash))
        return rnd

    def test_open_requires_verified_and_binds_snapshot(self):
        rnd = open_round(self.conn, task_id=self.task_id, actor="codex", role="implement")
        with self.assertRaisesRegex(Blocked, "round_not_verified"):
            open_review(self.conn, rnd["round_id"], "opus")
        self.conn.execute("UPDATE rounds SET state='VERIFIED',head_commit='a',head_tree='b' WHERE round_id=?", (rnd["round_id"],))
        result = open_review(self.conn, rnd["round_id"], "opus")
        row = self.conn.execute("SELECT * FROM reviews WHERE review_id=?", (result["review_id"],)).fetchone()
        self.assertEqual((row["state"], row["reviewed_commit"], row["reviewed_tree"], row["scope_id"]), ("WAITING", "a", "b", "none"))

    def test_independence_findings_and_idempotency(self):
        rnd = self.verified_round(actor="codex")
        opened = open_review(self.conn, rnd["round_id"], "opus")
        finding = {"id": "F1", "severity": "major", "path": "app.py", "symbol": "VALUE", "text": "check boundary"}
        result = submit_review(self.conn, review_id=opened["review_id"], actor="opus", verdict="REWORK_REQUIRED",
                               findings=[finding], client_request_id="review-1")
        self.assertEqual(result["independence"], "INDEPENDENT")
        replay = submit_review(self.conn, review_id=opened["review_id"], actor="opus", verdict="REWORK_REQUIRED",
                               findings=[finding], client_request_id="review-1")
        self.assertTrue(replay["replayed"])
        with self.assertRaises(IdempotencyConflict):
            submit_review(self.conn, review_id=opened["review_id"], actor="opus", verdict="PASS",
                          findings=[finding], client_request_id="review-1")
        row = self.conn.execute("SELECT state,verdict,independence,findings_json FROM reviews WHERE review_id=?", (opened["review_id"],)).fetchone()
        self.assertEqual((row["state"], row["verdict"], row["independence"]), ("REWORK_REQUIRED", "REWORK_REQUIRED", "INDEPENDENT"))
        self.assertIn("major", row["findings_json"])

    def test_self_reviewed_fix_and_invalid_finding(self):
        rnd = self.verified_round(actor="opus")
        opened = open_review(self.conn, rnd["round_id"], "opus")
        with self.assertRaises(ValidationError):
            submit_review(self.conn, review_id=opened["review_id"], actor="opus", verdict="PASS",
                          findings=[{"id": "F", "severity": "critical", "text": "bad"}], client_request_id="bad")
        result = submit_review(self.conn, review_id=opened["review_id"], actor="opus", verdict="PASS",
                               findings=[], client_request_id="good")
        self.assertEqual(result["independence"], "SELF_REVIEWED_FIX")

    def test_old_review_becomes_stale_when_new_round_is_submitted(self):
        first = self.verified_round()
        opened = open_review(self.conn, first["round_id"], "opus")
        submit_review(self.conn, review_id=opened["review_id"], actor="opus", verdict="PASS", findings=[], client_request_id="pass")
        second = open_round(self.conn, task_id=self.task_id, actor="codex", role="implement")
        second_head = commit_file(self.repo, "next.py", "VALUE = 3\n")
        submit_changes(self.conn, round_id=second["round_id"], actor="codex", commit=second_head, client_request_id="second")
        row = self.conn.execute("SELECT state,verdict FROM reviews WHERE review_id=?", (opened["review_id"],)).fetchone()
        self.assertEqual((row["state"], row["verdict"]), ("STALE", "PASS"))


if __name__ == "__main__":
    unittest.main()
