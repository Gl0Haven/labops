import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops.db import connect, migrate
from labops.errors import Blocked, ValidationError
from labops.tasks import add_repo, create_task, open_round, submit_changes
from labops.verify import normalize_profile, verify_round
from tests.helpers import commit_file, make_repo


def make_profile(commands=None, *, gpu=False, max_log=1024):
    return {"schema": 1, "commands": commands or [], "requires_gpu": gpu, "allowed_paths": ["**/*"], "max_log_bytes": max_log}


class VerifyTests(unittest.TestCase):
    def setup_case(self, *, trusted=True, profile=None, notes=None, suffix="x"):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        home = root / "home"
        repo = root / "repo"
        make_repo(repo, files={"src/sample.py": "VALUE = 1\n"})
        patcher = patch.dict(os.environ, {"LABOPS_HOME": str(home)})
        patcher.start()
        conn = connect(home / "labops.db")
        migrate(conn)
        add_repo(conn, repo_id="toy", path=repo, worktree_root=root / "worktrees", trusted_for_exec=trusted)
        task_id = f"T20261019-verify-{suffix}"
        create_task(conn, repo_id="toy", task_id=task_id, goal="verify", profile=profile or make_profile())
        rnd = open_round(conn, task_id=task_id, actor="codex", role="implement")
        head = commit_file(repo, "src/sample.py", "VALUE = 2\n")
        submit_changes(conn, round_id=rnd["round_id"], actor="codex", commit=head, client_request_id=f"req-{suffix}", notes=notes)
        return temp, patcher, conn, repo, rnd["round_id"]

    def close_case(self, case):
        temp, patcher, conn, *_ = case
        conn.close()
        patcher.stop()
        temp.cleanup()

    def run_case(self, **kwargs):
        case = self.setup_case(**kwargs)
        self.addCleanup(self.close_case, case)
        return case

    def test_passed_command_creates_independent_evidence(self):
        profile = make_profile([{"name": "unit", "argv": ["{python}", "-c", "print('passed')"], "cwd": ".", "timeout_s": 30}])
        case = self.run_case(profile=profile, suffix="pass")
        temp, patcher, conn, repo, rid = case
        result = verify_round(conn, rid)
        self.assertEqual((result["state"], result["status"]), ("VERIFIED", "passed"))
        evidence = conn.execute("SELECT kind,status,commit_oid,tree_oid,profile_hash,results_json,env_json FROM evidence WHERE evidence_id=?", (result["evidence_id"],)).fetchone()
        self.assertEqual((evidence["kind"], evidence["status"]), ("INDEPENDENTLY_EXECUTED", "passed"))
        self.assertTrue(json.loads(evidence["results_json"])[0]["log_sha256"])
        self.assertIn("executable", json.loads(evidence["env_json"]))

    def test_failure_skips_later_commands_and_rejects_round(self):
        profile = make_profile([
            {"name": "fail", "argv": ["{python}", "-c", "raise SystemExit(7)"], "cwd": ".", "timeout_s": 30},
            {"name": "later", "argv": ["{python}", "-c", "print('should not run')"], "cwd": ".", "timeout_s": 30},
        ])
        case = self.run_case(profile=profile, suffix="fail")
        _, _, conn, _, rid = case
        result = verify_round(conn, rid)
        self.assertEqual(result["state"], "REJECTED")
        self.assertEqual(result["results"][0]["exit_code"], 7)
        self.assertTrue(result["results"][1]["skipped"])

    def test_timeout_kills_command_and_marks_failure(self):
        profile = make_profile([{"name": "slow", "argv": ["{python}", "-c", "import time; time.sleep(10)"], "cwd": ".", "timeout_s": 2}])
        case = self.run_case(profile=profile, suffix="timeout")
        _, _, conn, _, rid = case
        result = verify_round(conn, rid)
        self.assertEqual(result["state"], "REJECTED")
        self.assertTrue(result["results"][0]["timed_out"])

    def test_missing_executable_blocks_round(self):
        profile = make_profile([{"name": "missing", "argv": ["labops-does-not-exist"], "cwd": ".", "timeout_s": 5}])
        case = self.run_case(profile=profile, suffix="missing")
        _, _, conn, _, rid = case
        result = verify_round(conn, rid)
        self.assertEqual(result["state"], "BLOCKED")
        row = conn.execute("SELECT state FROM rounds WHERE round_id=?", (rid,)).fetchone()
        self.assertEqual(row[0], "BLOCKED")
        self.assertEqual(conn.execute("SELECT status FROM evidence WHERE round_id=? AND kind='INDEPENDENTLY_EXECUTED'", (rid,)).fetchone()[0], "blocked")

    def test_untrusted_repo_and_gpu_requirement_block_without_running(self):
        profile = make_profile([{"name": "unit", "argv": ["{python}", "-c", "raise SystemExit(99)"], "cwd": ".", "timeout_s": 5}])
        untrusted = self.run_case(trusted=False, profile=profile, suffix="untrusted")
        _, _, conn, _, rid = untrusted
        with self.assertRaisesRegex(Blocked, "repo_not_trusted_for_exec"):
            verify_round(conn, rid)
        self.assertEqual(conn.execute("SELECT state FROM rounds WHERE round_id=?", (rid,)).fetchone()[0], "BLOCKED")
        gpu_case = self.run_case(trusted=True, profile=make_profile(profile["commands"], gpu=True), suffix="gpu")
        _, _, conn2, _, rid2 = gpu_case
        with self.assertRaisesRegex(Blocked, "requires_external_run"):
            verify_round(conn2, rid2)
        evidence = conn2.execute("SELECT status,results_json FROM evidence WHERE round_id=? AND kind='INDEPENDENTLY_EXECUTED'", (rid2,)).fetchone()
        self.assertEqual((evidence["status"], evidence["results_json"]), ("blocked", "[]"))

    def test_empty_profile_blocks_instead_of_passing(self):
        # GPT review P0: no commands means nothing was independently executed.
        _, _, conn, _, rid = self.run_case(profile=make_profile([]), suffix="empty")
        with self.assertRaisesRegex(Blocked, "no_checks"):
            verify_round(conn, rid)
        self.assertEqual(conn.execute("SELECT state FROM rounds WHERE round_id=?", (rid,)).fetchone()[0], "BLOCKED")
        statuses = [row[0] for row in conn.execute(
            "SELECT status FROM evidence WHERE round_id=? AND kind='INDEPENDENTLY_EXECUTED'", (rid,))]
        self.assertEqual(statuses, ["blocked"])

    def test_log_truncation_and_environment_whitelist(self):
        profile = make_profile([{"name": "output", "argv": ["{python}", "-c", "import os; print('x'*1000); print(os.getenv('SECRET_TOKEN'))"],
                                "cwd": ".", "timeout_s": 30}], max_log=64)
        case = self.run_case(profile=profile, suffix="env")
        _, _, conn, _, rid = case
        with patch.dict(os.environ, {"SECRET_TOKEN": "do-not-pass"}):
            result = verify_round(conn, rid)
        log = Path(result["results"][0]["log_path"]).read_bytes()
        self.assertIn(b"[labops: truncated]", log)
        self.assertNotIn(b"do-not-pass", log)
        self.assertLessEqual(len(log), 64 + len(b"\n[labops: truncated]\n"))

    def test_self_reported_tests_are_separate_evidence(self):
        notes = {"self_tests": {"passed": True, "summary": "agent said pass"}}
        case = self.run_case(notes=notes, suffix="self")
        _, _, conn, _, rid = case
        row = conn.execute("SELECT kind,status,results_json FROM evidence WHERE round_id=?", (rid,)).fetchone()
        self.assertEqual((row["kind"], row["status"]), ("SELF_REPORTED", "passed"))
        self.assertIn("agent said pass", row["results_json"])

    def test_profile_validation(self):
        valid = normalize_profile({"schema": 1, "commands": []})
        self.assertEqual(valid["max_log_bytes"], 10 * 1024 * 1024)
        invalids = [
            {"schema": 1, "commands": [{"name": "bad name", "argv": ["x"], "timeout_s": 1}]},
            {"schema": 1, "commands": [{"name": "x", "argv": ["{bad}"], "timeout_s": 1}]},
            {"schema": 1, "commands": [{"name": "x", "argv": ["x"], "timeout_s": 0}]},
            {"schema": 1, "commands": [{"name": "x", "argv": ["x"], "timeout_s": 1, "env": {"bad-key": "v"}}]},
        ]
        for profile in invalids:
            with self.subTest(profile=profile), self.assertRaises(ValidationError):
                normalize_profile(profile)


if __name__ == "__main__":
    unittest.main()
