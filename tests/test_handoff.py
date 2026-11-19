import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops.db import connect, migrate, tx
from labops.gitio import tree_of
from labops.handoff import build_handoff, handoff_data, render_handoff_md
from labops.review import open_review, submit_review
from labops.statemachine import transition
from labops.tasks import add_repo, create_task, create_worktree, open_round, set_policy, submit_changes
from tests.helpers import commit_file, git, make_repo


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        initial = "def add(a, b):\n    return a + b\n\ndef free():\n    return 1\n"
        make_repo(self.repo, files={"calc.py": initial, "tests/test_calc.py": "assert True\n"})
        self.patch = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.patch.start()
        self.conn = connect(self.home / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo, worktree_root=self.root / "worktrees")
        set_policy(self.conn, "toy", {"schema": 1, "frozen": [{"path": "calc.py", "symbols": ["add"], "ref": "§3.3", "note": "preserve add"}]})
        self.profile = {"schema": 1,
                        "commands": [{"name": "unit", "argv": ["{python}", "-m", "unittest", "discover"], "cwd": ".", "timeout_s": 60}],
                        "allowed_paths": ["calc.py", "tests/**"]}
        self.task_id = "T20261019-handoff"
        create_task(self.conn, repo_id="toy", task_id=self.task_id, goal="handoff reproducibility", profile=self.profile)

    def tearDown(self):
        self.conn.close()
        self.patch.stop()
        self.temp.cleanup()

    def _verified(self, rnd, head, evidence_id):
        profile_hash = self.conn.execute("SELECT profile_hash FROM tasks WHERE task_id=?", (self.task_id,)).fetchone()[0]
        with tx(self.conn):
            transition(self.conn, "round", rnd["round_id"], "VERIFYING", actor="test")
            transition(self.conn, "round", rnd["round_id"], "VERIFIED", actor="test")
            self.conn.execute("INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,results_json,producer,created_at,finished_at) "
                              "VALUES(?,?,'INDEPENDENTLY_EXECUTED','passed',?,?,?,?,'test','t','t')",
                              (evidence_id, rnd["round_id"], head, tree_of(self.repo, head), profile_hash,
                               json.dumps([{"name": "unit", "exit_code": 0, "duration_ms": 5, "timed_out": False,
                                            "log_sha256": "a" * 64, "log_path": "ignored"}])))

    def test_deterministic_complete_handoff_with_prior_findings_and_self_report(self):
        r1 = open_round(self.conn, task_id=self.task_id, actor="codex", role="implement")
        wt1 = Path(create_worktree(self.conn, r1["round_id"])["path"])
        head1 = commit_file(wt1, "calc.py", "def add(a, b):\n    return a + b\n\ndef free():\n    return 2\n")
        submit_changes(self.conn, round_id=r1["round_id"], actor="codex", commit=head1, client_request_id="r1")
        self._verified(r1, head1, "ev-r1")
        review = open_review(self.conn, r1["round_id"], "opus")
        finding = {"id": "F1", "severity": "major", "path": "calc.py", "symbol": "free", "text": "review this change"}
        submit_review(self.conn, review_id=review["review_id"], actor="opus", verdict="PASS_WITH_NOTES",
                      findings=[finding], client_request_id="r1-review")
        r2 = open_round(self.conn, task_id=self.task_id, actor="codex", role="fix")
        self.assertEqual(r2["base_commit"], head1)
        wt2 = Path(create_worktree(self.conn, r2["round_id"])["path"])
        head2 = commit_file(wt2, "calc.py", "def add(a, b):\n    return a + b\n\ndef free():\n    return 3\n")
        notes = {"summary": "changed free", "self_tests": {"passed": True, "items": ["unit"]}}
        submit_changes(self.conn, round_id=r2["round_id"], actor="codex", commit=head2, client_request_id="r2", notes=notes)
        self._verified(r2, head2, "ev-r2")
        data = handoff_data(self.conn, r2["round_id"])
        json_path, md_path = build_handoff(self.conn, r2["round_id"])
        first_json, first_md = json_path.read_bytes(), md_path.read_bytes()
        build_handoff(self.conn, r2["round_id"])
        self.assertEqual(json_path.read_bytes(), first_json)
        self.assertEqual(md_path.read_bytes(), first_md)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["round"]["head_commit"], head2)
        self.assertEqual(data["round"]["submitted_ref_value"], head2)
        self.assertEqual(data["acceptance"]["commands"][0]["name"], "unit")
        self.assertEqual(data["verification"]["status"], "passed")
        self.assertEqual(data["changes"]["totals"]["files"], 1)
        self.assertIn({"path": "calc.py", "qualname": "free"}, data["symbols"]["changed"])
        self.assertIn("F1", [item["id"] for item in data["prior_findings"]])
        self.assertEqual(data["self_reported"]["kind"], "SELF_REPORTED")
        self.assertEqual(data["self_reported"]["self_tests"]["passed"], True)
        self.assertEqual(data["next"], {"suggested_actor": "opus", "suggested_role": "code_review"})
        text = first_md.decode("utf-8")
        self.assertIn("已知限制（Agent 自报，未核验）", text)
        self.assertIn("review this change", text)

    def test_blocked_verification_command_is_listed_and_labeled(self):
        rnd = open_round(self.conn, task_id=self.task_id, actor="codex", role="implement")
        wt = Path(create_worktree(self.conn, rnd["round_id"])["path"])
        head = commit_file(wt, "calc.py", "def add(a, b):\n    return a + b + 1\n\ndef free():\n    return 1\n")
        from labops.tasks import submit_changes
        submit_changes(self.conn, round_id=rnd["round_id"], actor="codex", commit=head, client_request_id="blocked")
        profile_hash = self.conn.execute("SELECT profile_hash FROM tasks WHERE task_id=?", (self.task_id,)).fetchone()[0]
        results = [{"name": "missing", "argv": ["no-such-command"], "exit_code": None, "duration_ms": 0,
                    "timed_out": False, "log_sha256": "blocked-hash"},
                   {"name": "later", "argv": ["python"], "exit_code": None, "duration_ms": 0,
                    "timed_out": False, "skipped": True, "log_sha256": None}]
        self.conn.execute("INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,results_json,producer,created_at,finished_at) "
                          "VALUES('ev-blocked',?,'INDEPENDENTLY_EXECUTED','blocked',?,?,?,?,'labops','t','t')",
                          (rnd["round_id"], head, tree_of(self.repo, head), profile_hash, json.dumps(results)))
        data = handoff_data(self.conn, rnd["round_id"])
        self.assertFalse(data["verification"]["results"][0]["skipped"])
        self.assertTrue(data["verification"]["results"][1]["skipped"])
        markdown = render_handoff_md(data)
        failures = markdown.split("### 验证失败命令", 1)[1]
        self.assertIn("`missing`：blocked", failures)
        self.assertNotIn("`later`", failures)

    def test_binary_numstat_totals_are_null_and_marked(self):
        rnd = open_round(self.conn, task_id=self.task_id, actor="codex", role="implement")
        wt = Path(create_worktree(self.conn, rnd["round_id"])["path"])
        binary = wt / "tests" / "data.bin"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"\x00\x01\xff\x00")
        git(wt, "add", "--", "tests/data.bin")
        git(wt, "commit", "-m", "add binary")
        head = git(wt, "rev-parse", "HEAD").stdout.decode().strip()
        from labops.tasks import submit_changes
        submit_changes(self.conn, round_id=rnd["round_id"], actor="codex", commit=head, client_request_id="binary")
        data = handoff_data(self.conn, rnd["round_id"])
        item = next(row for row in data["changes"]["files"] if row["path"] == "tests/data.bin")
        self.assertIsNone(item["added"])
        self.assertIsNone(item["deleted"])
        self.assertIsNone(data["changes"]["totals"]["added"])
        self.assertIsNone(data["changes"]["totals"]["deleted"])
        markdown = render_handoff_md(data)
        self.assertIn("含二进制，未汇总", markdown)


if __name__ == "__main__":
    unittest.main()
