import json
import tempfile
import unittest
from pathlib import Path

from labops.db import connect, migrate, tx
from labops.errors import ValidationError
from labops.history import query_history
from labops.tasks import add_repo, create_task, open_round, submit_changes
from tests.helpers import commit_file, make_repo


class QueryHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-history-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.base = make_repo(self.repo)
        self.conn = connect(self.root / "home" / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo)
        self.task_id = "T20261019-history"
        created = create_task(self.conn, repo_id="toy", task_id=self.task_id, goal="history", profile={"schema": 1})
        self.round = open_round(self.conn, task_id=self.task_id, actor="codex", role="implement")
        self.commit = commit_file(self.repo, "result.txt", "result\n")
        self.claim = submit_changes(self.conn, round_id=self.round["round_id"], actor="codex", commit=self.commit,
                                    client_request_id="history-1", notes={"self_tests": ["unit"]})
        with tx(self.conn):
            self.conn.execute(
                "INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,results_json,env_json,producer,created_at) "
                "VALUES('ev1',?,'INDEPENDENTLY_EXECUTED','passed',?,'tree',?,'[]','{\"secret\":true}','test','t')",
                (self.round["round_id"], self.commit, created["profile_hash"]),
            )
            self.conn.execute(
                "INSERT INTO reviews(review_id,task_id,round_id,reviewer,state,verdict,independence,base_commit,reviewed_commit,reviewed_tree,scope_id,acceptance_profile_hash,findings_json,created_at,updated_at) "
                "VALUES('rv1',?,?,'opus','SUBMITTED','PASS','INDEPENDENT',? ,?,'tree','scope','profile','[{\"id\":\"f1\"},{\"id\":\"f2\"}]','t','t')",
                (self.task_id, self.round["round_id"], self.base, self.commit),
            )

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_task_history_omits_sensitive_columns_and_limits_rows(self):
        history = query_history(self.conn, task_id=self.task_id, limit=1)
        self.assertEqual(history["task_id"], self.task_id)
        self.assertEqual(len(history["rounds"]), 1)
        self.assertEqual(history["claims"][0]["claim_id"], self.claim["claim_id"])
        self.assertNotIn("payload_json", history["claims"][0])
        self.assertEqual(history["reviews"][0]["findings_count"], 2)
        self.assertNotIn("findings_json", history["reviews"][0])
        self.assertNotIn("env_json", history["evidence"][0])
        self.assertEqual(history["evidence"][0]["commit_oid"], self.commit)
        self.assertEqual(history["events"], sorted(history["events"], key=lambda item: item["seq"]))

    def test_sha_prefix_matches_commit_and_claim_fingerprint(self):
        result = query_history(self.conn, sha=self.commit[:10])
        self.assertFalse(result["ambiguous"])
        self.assertTrue(any(item["table"] == "rounds" and item["column"] == "head_commit" for item in result["matches"]))
        self.assertTrue(any(item["table"] == "claims" and item["column"] == "fingerprint" for item in result["matches"]))

    def test_multiple_different_full_sha_values_mark_prefix_ambiguous(self):
        first = "abcdef0" + "1" * 33
        second = "abcdef0" + "2" * 33
        with tx(self.conn):
            self.conn.execute("UPDATE rounds SET base_commit=?,head_commit=? WHERE round_id=?",
                              (first, second, self.round["round_id"]))
        result = query_history(self.conn, sha="abcdef0")
        self.assertTrue(result["ambiguous"])

    def test_invalid_selector_limit_and_sha_are_rejected(self):
        for kwargs in ({}, {"task_id": self.task_id, "sha": "abcdef0"}, {"task_id": self.task_id, "limit": 0}, {"sha": "xyz"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                query_history(self.conn, **kwargs)

    def test_each_category_limit_scans_all_task_rounds(self):
        task_id = "T20261019-history-limit"
        created = create_task(self.conn, repo_id="toy", task_id=task_id, goal="category limits", profile={"schema": 1})
        first = open_round(self.conn, task_id=task_id, actor="codex", role="implement")
        self.conn.execute("UPDATE rounds SET state='BLOCKED' WHERE round_id=?", (first["round_id"],))
        second = open_round(self.conn, task_id=task_id, actor="opus", role="fix")
        head = commit_file(self.repo, "limit-result.txt", "limit result\n")
        submit_changes(self.conn, round_id=second["round_id"], actor="opus", commit=head,
                       client_request_id="limit-claim")
        self.conn.execute(
            "INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,producer,created_at) "
            "VALUES('limit-evidence',?,'INDEPENDENTLY_EXECUTED','passed',?,'tree',?,'test','t')",
            (second["round_id"], head, created["profile_hash"]),
        )
        history = query_history(self.conn, task_id=task_id, limit=1)
        self.assertEqual([row["round_id"] for row in history["rounds"]], [first["round_id"]])
        self.assertEqual([row["round_id"] for row in history["claims"]], [second["round_id"]])
        self.assertEqual([row["round_id"] for row in history["evidence"]], [second["round_id"]])


if __name__ == "__main__":
    unittest.main()
