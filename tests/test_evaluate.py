import json
import tempfile
import unittest
from pathlib import Path

from labops.db import connect, migrate
from labops.errors import ValidationError
from labops.evaluate import check_eval_file, evaluate_retrieval
from labops.tasks import add_repo
from tests.helpers import make_repo


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-evaluate-")
        self.root = Path(self.temp.name)
        self.repo_a = self.root / "repo-a"
        self.repo_b = self.root / "repo-b"
        self.base_a = make_repo(self.repo_a)
        self.base_b = make_repo(self.repo_b)
        self.conn = connect(self.root / "home" / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="a", path=self.repo_a)
        add_repo(self.conn, repo_id="b", path=self.repo_b)
        self._index("doc-a", "a", "D-20261020-1", "alpha result")
        self._index("doc-b", "b", "D-20261020-2", "beta result")
        self.question_file = self.root / "questions.jsonl"
        self.question_file.write_text("\n".join((
            json.dumps({"id": "q1", "query": "D-20261020-1", "expected": [
                {"repo_id": "a", "ref": "D-20261020-1"}, {"repo_id": "b", "ref": "D-20261020-2"},
            ]}),
            json.dumps({"id": "q2", "query": "D-20261020-2", "expected": [
                {"repo_id": "b", "ref": "D-20261020-2-missing"},
            ]}),
            json.dumps({"id": "q3", "query": "D-20261020-missing", "expected": [
                {"repo_id": "a", "ref": "D-20261020-missing"},
            ]}),
        )) + "\n", encoding="utf-8")

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _index(self, doc_id, repo_id, ref, body, superseded_by=None):
        self.conn.execute(
            "INSERT INTO search_docs(doc_id,repo_id,kind,ref,path,line,title,body,ts,content_hash,superseded_by,confidential,indexed_at) "
            "VALUES(?,?,'decision',?,NULL,NULL,?,?,NULL,'hash',?,0,'2026-10-20T00:00:00Z')",
            (doc_id, repo_id, ref, ref, body, superseded_by),
        )

    def test_repo_filter_limits_results_and_expected_to_selected_repo(self):
        output = self.root / "eval-out"
        result = evaluate_retrieval(self.conn, file=self.question_file, ks=[1], out=output, repo_filter="a")
        report = result["report"]
        self.assertEqual(report["repo_filter"], "a")
        self.assertEqual(report["questions"], 2)
        self.assertEqual(report["metrics"]["recall@1"], 0.5)
        self.assertEqual(report["results"][0]["expected"], [("a", "D-20261020-1")])
        self.assertTrue(all(item["repo_id"] == "a" for row in report["results"] for item in row["results"]))

    def test_check_file_lists_missing_expected_refs_and_line_numbers(self):
        result = check_eval_file(self.conn, file=self.question_file)
        self.assertFalse(result["ok"])
        self.assertEqual(result["questions"], 3)
        self.assertEqual(result["expected"], 4)
        self.assertEqual(result["missing"], [
            {"line": 2, "id": "q2", "repo_id": "b", "ref": "D-20261020-2-missing"},
            {"line": 3, "id": "q3", "repo_id": "a", "ref": "D-20261020-missing"},
        ])

    def test_check_file_ignores_superseded_index_docs(self):
        self._index("doc-old", "a", "D-20261020-old", "old", superseded_by="D-20261020-new")
        questions = self.root / "old.jsonl"
        questions.write_text(json.dumps({"id": "old", "query": "old", "expected": [
            {"repo_id": "a", "ref": "D-20261020-old"},
        ]}) + "\n", encoding="utf-8")
        self.assertFalse(check_eval_file(self.conn, file=questions)["ok"])

    def test_repo_filter_validates_registered_repo_and_matching_questions(self):
        with self.assertRaisesRegex(ValidationError, "eval_repo_not_found"):
            evaluate_retrieval(self.conn, file=self.question_file, repo_filter="missing")
        only_b = self.root / "b.jsonl"
        only_b.write_text(json.dumps({"id": "b", "query": "beta", "expected": [
            {"repo_id": "b", "ref": "D-20261020-2"},
        ]}) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "empty_eval_question_set_after_repo_filter"):
            evaluate_retrieval(self.conn, file=only_b, repo_filter="a")


if __name__ == "__main__":
    unittest.main()
