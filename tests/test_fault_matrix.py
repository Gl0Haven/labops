import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT, commit_file, git, make_repo, run_cli


class FaultMatrixTests(unittest.TestCase):
    def case(self, slug: str, *, commands=None):
        temp = tempfile.TemporaryDirectory(prefix=f"labops-{slug}-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        home, repo = root / "home", root / "toy"
        make_repo(repo, files={"calc.py": "def add(a,b):\n    return a+b\n", "tests/test_calc.py": "assert True\n"})
        profile_path = root / "profile.json"
        profile = {"schema": 1, "commands": commands or [{"name": "unit", "argv": ["{python}", "-c", "pass"],
                                                            "cwd": ".", "timeout_s": 30}],
                   "allowed_paths": ["**"]}
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        self.ok(home, "init", "--test-instance")
        self.ok(home, "repo", "add", "--repo-id", "toy", "--path", str(repo), "--worktree-root", str(root / "wts"),
                "--trusted-for-exec", extra_env={"LABOPS_TEST_CONFIRM": "toy"})
        task_id = f"T20261019-{slug}"
        self.ok(home, "task", "create", "--repo-id", "toy", "--task-id", task_id, "--goal", f"fault {slug}",
                "--profile", str(profile_path))
        return {"root": root, "home": home, "repo": repo, "task": task_id, "profile": profile_path}

    def call(self, case, *args, extra_env=None):
        return run_cli(case["home"], *args, cwd=case["repo"], extra_env=extra_env)

    def ok(self, home, *args, extra_env=None):
        result = run_cli(home, *args, cwd=ROOT, extra_env=extra_env)
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout.decode("utf-8"))

    def ok_case(self, case, *args, extra_env=None):
        result = self.call(case, *args, extra_env=extra_env)
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout.decode("utf-8"))

    def open_round(self, case, *, actor="codex", role="implement"):
        result = self.ok_case(case, "round", "open", "--task-id", case["task"], "--actor", actor, "--role", role)
        return result["round_id"]

    def create_wt(self, case, round_id):
        result = self.ok_case(case, "wt", "create", "--round", round_id)
        self.assertEqual(result["status"], "APPLIED")
        return Path(result["path"])

    def snapshot(self, case, round_id, wt, text="def add(a,b):\n    return a+b+1\n", *, message="change", extra_env=None):
        (wt / "calc.py").write_bytes(text.encode("utf-8"))
        result = self.ok_case(case, "snapshot", "--round", round_id, "--actor", "codex", "--message", message,
                              "--files", "calc.py", extra_env=extra_env)
        return result["commit"]

    def submit(self, case, round_id, commit, request="req", *, notes=None, extra_env=None):
        args = ["submit", "--round", round_id, "--actor", "codex", "--commit", commit, "--request-id", request]
        if notes is not None:
            notes_path = case["root"] / f"{request}.json"
            notes_path.write_text(json.dumps(notes), encoding="utf-8")
            args += ["--notes", str(notes_path)]
        return self.call(case, *args, extra_env=extra_env)

    def init_round_with_snapshot(self, slug, *, commands=None):
        case = self.case(slug, commands=commands)
        round_id = self.open_round(case)
        wt = self.create_wt(case, round_id)
        head = self.snapshot(case, round_id, wt)
        return case, round_id, wt, head

    def reconcile_twice(self, case):
        first = self.ok_case(case, "reconcile", "--repo-id", "toy")
        second = self.ok_case(case, "reconcile", "--repo-id", "toy")
        self.assertTrue(all(value == 0 for key, value in second.items() if key != "ok"), second)
        return first

    def setup_ready_integration(self, slug):
        case, round_id, wt, head = self.init_round_with_snapshot(slug)
        self.assertEqual(self.submit(case, round_id, head).returncode, 0)
        self.ok_case(case, "verify", "--round", round_id)
        review = self.ok_case(case, "review", "open", "--round", round_id, "--reviewer", "opus")
        finding_path = case["root"] / "findings.json"
        finding_path.write_text("[]", encoding="utf-8")
        self.ok_case(case, "review", "submit", "--review", review["review_id"], "--actor", "opus", "--verdict", "PASS",
                     "--request-id", "review", "--findings", str(finding_path))
        gate = self.ok_case(case, "gate", "check", "--task-id", case["task"])
        self.assertTrue(gate["pass"], gate)
        integration = self.ok_case(case, "integrate", "propose", "--task-id", case["task"])
        self.assertEqual(integration["state"], "AWAITING_HUMAN_APPROVAL")
        return case, round_id, integration

    def test_F1_submit_crash_after_intent(self):
        case, rid, _, head = self.init_round_with_snapshot("f1")
        result = self.submit(case, rid, head, extra_env={"LABOPS_CRASHPOINT": "op_after_intent"})
        self.assertEqual(result.returncode, 97)
        first = self.reconcile_twice(case)
        self.assertEqual(first["operations_retried"], 1)
        status = self.ok_case(case, "status", "--task-id", case["task"])
        self.assertEqual(status["rounds"][0]["state"], "CLAIMED")

    def test_F2_submit_crash_after_git_is_not_retried(self):
        case, rid, _, head = self.init_round_with_snapshot("f2")
        result = self.submit(case, rid, head, extra_env={"LABOPS_CRASHPOINT": "op_after_git"})
        self.assertEqual(result.returncode, 97)
        self.reconcile_twice(case)
        conn = __import__("sqlite3").connect(case["home"] / "labops.db")
        detail = conn.execute("SELECT detail_json FROM operations WHERE kind='create_ref' AND idempotency_id LIKE 'submit:%'").fetchone()[0]
        self.assertNotIn("retried", detail or "")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM operations WHERE idempotency_id LIKE 'submit:%'").fetchone()[0], 1)
        conn.close()

    def test_F3_submit_crash_after_record(self):
        case, rid, _, head = self.init_round_with_snapshot("f3")
        result = self.submit(case, rid, head, extra_env={"LABOPS_CRASHPOINT": "op_after_record"})
        self.assertEqual(result.returncode, 97)
        first = self.reconcile_twice(case)
        self.assertTrue(all(value == 0 for key, value in first.items() if key != "ok"), first)
        status = self.ok_case(case, "status", "--task-id", case["task"])
        self.assertEqual(status["rounds"][0]["state"], "CLAIMED")

    def test_F4_same_request_same_payload_replays(self):
        case, rid, _, head = self.init_round_with_snapshot("f4")
        first = self.ok_case(case, "submit", "--round", rid, "--actor", "codex", "--commit", head, "--request-id", "same")
        second = self.ok_case(case, "submit", "--round", rid, "--actor", "codex", "--commit", head, "--request-id", "same")
        self.assertEqual(first["claim_id"], second["claim_id"])
        self.assertTrue(second["replayed"])
        conn = __import__("sqlite3").connect(case["home"] / "labops.db")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM claims WHERE status='accepted'").fetchone()[0], 1)
        conn.close()

    def test_F5_same_request_different_payload_conflicts(self):
        case, rid, _, head = self.init_round_with_snapshot("f5")
        self.ok_case(case, "submit", "--round", rid, "--actor", "codex", "--commit", head, "--request-id", "same")
        notes_path = case["root"] / "different.json"
        notes_path.write_text('{"note":"different"}', encoding="utf-8")
        result = self.call(case, "submit", "--round", rid, "--actor", "codex", "--commit", head, "--request-id", "same", "--notes", str(notes_path))
        self.assertEqual(result.returncode, 4)
        self.assertEqual(json.loads(result.stdout)["error"], "idempotency_conflict")

    def test_F6_concurrent_different_shas_only_one_claims_round(self):
        case = self.case("f6")
        rid = self.open_round(case)
        wt = self.create_wt(case, rid)
        sha1 = commit_file(wt, "calc.py", "def add(a,b):\n    return a+b+1\n", "first")
        sha2 = commit_file(wt, "calc.py", "def add(a,b):\n    return a+b+2\n", "second")
        env = os.environ.copy(); env["LABOPS_HOME"] = str(case["home"]); env["PYTHONPATH"] = str(ROOT / "src")
        commands = [[sys.executable, "-m", "labops.cli", "submit", "--round", rid, "--actor", "codex", "--commit", sha1, "--request-id", "one"],
                    [sys.executable, "-m", "labops.cli", "submit", "--round", rid, "--actor", "codex", "--commit", sha2, "--request-id", "two"]]
        procs = [subprocess.Popen(cmd, cwd=case["repo"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for cmd in commands]
        outputs = [proc.communicate(timeout=20) + (proc.returncode,) for proc in procs]
        codes = [item[2] for item in outputs]
        self.assertEqual(sorted(codes), [0, 4])
        results = [json.loads(item[0].decode("utf-8")) for item in outputs]
        self.assertIn("version_conflict", [item.get("error") for item in results])
        conn = __import__("sqlite3").connect(case["home"] / "labops.db")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM claims WHERE status='accepted'").fetchone()[0], 1)
        conn.close()

    def test_F7_different_requests_same_sha_are_duplicate(self):
        case, rid, _, head = self.init_round_with_snapshot("f7")
        first = self.ok_case(case, "submit", "--round", rid, "--actor", "codex", "--commit", head, "--request-id", "one")
        second = self.ok_case(case, "submit", "--round", rid, "--actor", "codex", "--commit", head, "--request-id", "two")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["duplicate_of"], first["claim_id"])

    def test_F8_manual_submitted_ref_change_is_preserved(self):
        case, rid, _, head = self.init_round_with_snapshot("f8")
        self.ok_case(case, "submit", "--round", rid, "--actor", "codex", "--commit", head, "--request-id", "one")
        other = commit_file(case["repo"], "main-only.txt", "new\n")
        ref = f"refs/labops/{case['task']}/r1/codex/submitted"
        self.assertTrue(git(case["repo"], "update-ref", ref, other, head).returncode == 0)
        first = self.reconcile_twice(case)
        self.assertEqual(first["integrity_conflicts"], 1)
        gate = self.ok_case(case, "gate", "check", "--task-id", case["task"])
        self.assertIn("submitted_ref_mismatch", gate["reasons"])
        self.assertEqual(git(case["repo"], "rev-parse", ref).stdout.decode().strip(), other)

    def test_F9_worktree_crash_after_add_recovers_record(self):
        case = self.case("f9")
        rid = self.open_round(case)
        crashed = self.call(case, "wt", "create", "--round", rid, extra_env={"LABOPS_CRASHPOINT": "wt_after_add"})
        self.assertEqual(crashed.returncode, 97)
        first = self.reconcile_twice(case)
        self.assertEqual(first["operations_applied"], 1)
        status = self.ok_case(case, "status", "--task-id", case["task"])
        self.assertTrue(status["rounds"][0]["branch"])
        self.assertTrue(Path(status["rounds"][0]["worktree_path"]).is_dir())

    def test_F10_snapshot_crash_after_add_keeps_staged_files(self):
        case = self.case("f10")
        rid, wt = None, None
        rid = self.open_round(case); wt = self.create_wt(case, rid)
        (wt / "calc.py").write_bytes(b"def add(a,b):\n    return a+b+1\n")
        crashed = self.call(case, "snapshot", "--round", rid, "--actor", "codex", "--message", "snap", "--files", "calc.py",
                            extra_env={"LABOPS_CRASHPOINT": "snap_after_add"})
        self.assertEqual(crashed.returncode, 97)
        first = self.reconcile_twice(case)
        self.assertEqual(first["operations_failed"], 1)
        status = git(wt, "status", "--porcelain").stdout.decode("utf-8", "replace")
        self.assertTrue(status.startswith("M "), status)
        conn = __import__("sqlite3").connect(case["home"] / "labops.db")
        detail = conn.execute("SELECT detail_json FROM operations WHERE kind='snapshot_commit'").fetchone()[0]
        self.assertIn("interrupted_before_commit", detail)
        conn.close()

    def test_F11_snapshot_crash_after_commit_recovers_target(self):
        case = self.case("f11"); rid = self.open_round(case); wt = self.create_wt(case, rid)
        (wt / "calc.py").write_bytes(b"def add(a,b):\n    return a+b+1\n")
        crashed = self.call(case, "snapshot", "--round", rid, "--actor", "codex", "--message", "snap", "--files", "calc.py",
                            extra_env={"LABOPS_CRASHPOINT": "snap_after_commit"})
        self.assertEqual(crashed.returncode, 97)
        target = git(wt, "rev-parse", "HEAD").stdout.decode().strip()
        first = self.reconcile_twice(case)
        self.assertEqual(first["operations_applied"], 1)
        conn = __import__("sqlite3").connect(case["home"] / "labops.db")
        op_target, op_status = conn.execute("SELECT target,status FROM operations WHERE kind='snapshot_commit'").fetchone()
        conn.close()
        self.assertEqual((op_target, op_status), (target, "APPLIED"))

    def test_F12_index_lock_blocks_and_preserves_lock(self):
        case = self.case("f12"); rid = self.open_round(case); wt = self.create_wt(case, rid)
        lock_value = git(wt, "rev-parse", "--git-path", "index.lock").stdout.decode().strip()
        lock = Path(lock_value)
        if not lock.is_absolute():
            lock = wt / lock
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_bytes(b"preserve")
        (wt / "calc.py").write_bytes(b"def add(a,b):\n    return a+b+1\n")
        try:
            result = self.call(case, "snapshot", "--round", rid, "--actor", "codex", "--message", "snap", "--files", "calc.py")
            self.assertEqual(result.returncode, 5)
            self.assertEqual(json.loads(result.stdout)["message"], "index_lock_present")
            self.assertEqual(lock.read_bytes(), b"preserve")
        finally:
            lock.unlink()

    def test_F13_verify_crash_reconcile_and_retry(self):
        commands = [{"name": "unit", "argv": ["{python}", "-c", "import time; time.sleep(0.1)"], "cwd": ".", "timeout_s": 20}]
        case, rid, _, head = self.init_round_with_snapshot("f13", commands=commands)
        self.ok_case(case, "submit", "--round", rid, "--actor", "codex", "--commit", head, "--request-id", "submit")
        crashed = self.call(case, "verify", "--round", rid, extra_env={"LABOPS_CRASHPOINT": "verify_after_run"})
        self.assertEqual(crashed.returncode, 97)
        first = self.reconcile_twice(case)
        self.assertEqual(first["verification_interrupted"], 1)
        status = self.ok_case(case, "status", "--task-id", case["task"])
        self.assertEqual(status["rounds"][0]["state"], "BLOCKED")
        result = self.ok_case(case, "verify", "--round", rid, "--retry")
        self.assertEqual(result["state"], "VERIFIED")

    def test_F14_ff_after_intent_is_not_automatically_merged_and_can_retry(self):
        case, _, integration = self.setup_ready_integration("f14")
        confirmation = integration["candidate_tree"][:12]
        crashed = self.call(case, "integrate", "approve", "--integration", integration["integration_id"],
                            extra_env={"LABOPS_TEST_CONFIRM": confirmation, "LABOPS_CRASHPOINT": "ff_after_intent"})
        self.assertEqual(crashed.returncode, 97)
        first = self.reconcile_twice(case)
        self.assertEqual(first["operations_failed"], 1)
        main = git(case["repo"], "rev-parse", "refs/heads/main").stdout.decode().strip()
        self.assertNotEqual(main, integration["candidate_commit"])
        state = self.ok_case(case, "status", "--task-id", case["task"])
        self.assertEqual(state["integrations"][0]["state"], "AWAITING_HUMAN_APPROVAL")
        retried = self.ok_case(case, "integrate", "approve", "--integration", integration["integration_id"],
                               extra_env={"LABOPS_TEST_CONFIRM": confirmation})
        self.assertEqual(retried["state"], "MERGED")

    def test_F15_ff_after_merge_reconcile_completes_integration(self):
        case, _, integration = self.setup_ready_integration("f15")
        crashed = self.call(case, "integrate", "approve", "--integration", integration["integration_id"],
                            extra_env={"LABOPS_TEST_CONFIRM": integration["candidate_tree"][:12], "LABOPS_CRASHPOINT": "ff_after_merge"})
        self.assertEqual(crashed.returncode, 97)
        first = self.reconcile_twice(case)
        self.assertEqual(first["operations_applied"], 1)
        status = self.ok_case(case, "status", "--task-id", case["task"])
        self.assertEqual((status["task"]["state"], status["integrations"][0]["state"]), ("INTEGRATED", "MERGED"))
        conn = __import__("sqlite3").connect(case["home"] / "labops.db")
        consumed = conn.execute("SELECT consumed_at FROM approvals WHERE integration_id=?", (integration["integration_id"],)).fetchone()[0]
        conn.close()
        self.assertIsNotNone(consumed)

    def test_F16_main_advance_before_approval_marks_stale(self):
        case, _, integration = self.setup_ready_integration("f16")
        advanced = commit_file(case["repo"], "main-advance.txt", "advance\n")
        result = self.ok_case(case, "integrate", "approve", "--integration", integration["integration_id"],
                              extra_env={"LABOPS_TEST_CONFIRM": integration["candidate_tree"][:12]})
        self.assertEqual((result["state"], result["reason"]), ("STALE", "main_advanced"))
        self.assertEqual(git(case["repo"], "rev-parse", "refs/heads/main").stdout.decode().strip(), advanced)


if __name__ == "__main__":
    unittest.main()
