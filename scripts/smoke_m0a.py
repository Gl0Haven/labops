import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "examples" / "profile.toy.json"
POLICY = ROOT / "examples" / "policy.toy.json"


def _run(argv, *, cwd, env, expected=0):
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True)
    if result.returncode != expected:
        raise RuntimeError(f"command failed ({result.returncode}): {argv}\nstdout={result.stdout.decode('utf-8','replace')}\nstderr={result.stderr.decode('utf-8','replace')}")
    if expected != 0:
        return None
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command did not return JSON: {argv}\n{result.stdout!r}") from exc
    if not payload.get("ok"):
        raise RuntimeError(f"command returned an error: {argv}\n{payload}")
    return payload


def _cli(env, cwd, *args, expected=0, extra=None):
    child_env = env.copy()
    if extra:
        child_env.update(extra)
    return _run([sys.executable, "-m", "labops.cli", *map(str, args)], cwd=cwd, env=child_env, expected=expected)


def _git(repo: Path, *args: str):
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    result = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"git failed ({result.returncode}): {args}\n{result.stderr.decode('utf-8','replace')}")
    return result.stdout.decode("utf-8", "surrogateescape").strip()


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="labops-smoke-"))
    home, repo = root / "home", root / "toy"
    repo.mkdir()
    env = os.environ.copy()
    env["LABOPS_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT / "src")
    _run([sys.executable, "-m", "labops.cli", "init", "--test-instance"], cwd=ROOT, env=env)
    _git(repo, "-c", "init.defaultBranch=main", "init")
    _git(repo, "config", "user.name", "labops-test")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    _write(repo / "calc.py", "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
    _write(repo / "tests" / "__init__.py", "")
    _write(repo / "tests" / "test_calc.py", "import unittest\nfrom calc import add, mul\n\nclass CalcTests(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\nif __name__ == '__main__':\n    unittest.main()\n")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-m", "initial toy")
    _cli(env, repo, "repo", "add", "--repo-id", "toy", "--path", repo, "--worktree-root", root / "worktrees",
         "--trusted-for-exec", extra={"LABOPS_TEST_CONFIRM": "toy"})
    _cli(env, repo, "policy", "set", "--repo-id", "toy", "--file", POLICY)
    task_id = "T20261019-smoke"
    _cli(env, repo, "task", "create", "--repo-id", "toy", "--task-id", task_id, "--goal", "Fix multiplication with review and integration", "--profile", PROFILE)

    rounds = []
    # Round 1 deliberately contains an incorrect multiplication implementation.
    r1 = _cli(env, repo, "round", "open", "--task-id", task_id, "--actor", "codex", "--role", "implement")["round_id"]
    wt1 = Path(_cli(env, repo, "wt", "create", "--round", r1)["path"])
    _write(wt1 / "calc.py", "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a + b\n")
    _write(wt1 / "tests" / "test_calc.py", "import unittest\nfrom calc import add, mul\n\nclass CalcTests(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n    def test_mul(self):\n        self.assertEqual(mul(2, 3), 6)\n\nif __name__ == '__main__':\n    unittest.main()\n")
    h1 = _cli(env, wt1, "snapshot", "--round", r1, "--actor", "codex", "--message", "r1 incorrect mul", "--files", "calc.py", "tests/test_calc.py")["commit"]
    _cli(env, wt1, "submit", "--round", r1, "--actor", "codex", "--commit", h1, "--request-id", "r1")
    v1 = _cli(env, wt1, "verify", "--round", r1)
    if v1["state"] != "REJECTED":
        raise RuntimeError(f"r1 should fail verification: {v1}")
    rounds.append(r1)

    # Round 2 fixes the test failure; Opus review requests rework.
    r2 = _cli(env, repo, "round", "open", "--task-id", task_id, "--actor", "codex", "--role", "implement")["round_id"]
    wt2 = Path(_cli(env, repo, "wt", "create", "--round", r2)["path"])
    _write(wt2 / "calc.py", "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
    h2 = _cli(env, wt2, "snapshot", "--round", r2, "--actor", "codex", "--message", "r2 fix mul", "--files", "calc.py")["commit"]
    _cli(env, wt2, "submit", "--round", r2, "--actor", "codex", "--commit", h2, "--request-id", "r2")
    v2 = _cli(env, wt2, "verify", "--round", r2)
    if v2["state"] != "VERIFIED":
        raise RuntimeError(f"r2 should pass verification: {v2}")
    review2 = _cli(env, wt2, "review", "open", "--round", r2, "--reviewer", "opus")["review_id"]
    findings = root / "r2-findings.json"
    findings.write_text(json.dumps([{"id": "R2-1", "severity": "major", "path": "calc.py", "symbol": "mul", "text": "Add an explicit zero boundary case."}]), encoding="utf-8")
    _cli(env, wt2, "review", "submit", "--review", review2, "--actor", "opus", "--verdict", "REWORK_REQUIRED", "--request-id", "r2-review", "--findings", findings)
    gate2 = _cli(env, wt2, "gate", "check", "--task-id", task_id)
    if gate2["pass"] or not {"open_blocking_review", "no_pass_review"}.issubset(set(gate2["reasons"])):
        raise RuntimeError(f"r2 gate did not block: {gate2}")
    rounds.append(r2)

    # Round 3 is an Opus fix. Its self-review requires a separate user approval.
    r3 = _cli(env, repo, "round", "open", "--task-id", task_id, "--actor", "opus", "--role", "fix")["round_id"]
    wt3 = Path(_cli(env, repo, "wt", "create", "--round", r3)["path"])
    _write(wt3 / "calc.py", "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    if a == 0 or b == 0:\n        return 0\n    return a * b\n")
    _write(wt3 / "tests" / "test_calc.py", "import unittest\nfrom calc import add, mul\n\nclass CalcTests(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n    def test_mul(self):\n        self.assertEqual(mul(2, 3), 6)\n\n    def test_mul_zero(self):\n        self.assertEqual(mul(0, 5), 0)\n\nif __name__ == '__main__':\n    unittest.main()\n")
    h3 = _cli(env, wt3, "snapshot", "--round", r3, "--actor", "opus", "--message", "r3 zero boundary", "--files", "calc.py", "tests/test_calc.py")["commit"]
    _cli(env, wt3, "submit", "--round", r3, "--actor", "opus", "--commit", h3, "--request-id", "r3")
    v3 = _cli(env, wt3, "verify", "--round", r3)
    if v3["state"] != "VERIFIED":
        raise RuntimeError(f"r3 should pass verification: {v3}")
    review3 = _cli(env, wt3, "review", "open", "--round", r3, "--reviewer", "opus")["review_id"]
    _cli(env, wt3, "review", "submit", "--review", review3, "--actor", "opus", "--verdict", "PASS", "--request-id", "r3-review")
    gate3 = _cli(env, wt3, "gate", "check", "--task-id", task_id)
    if "self_review_needs_acceptance" not in gate3["reasons"]:
        raise RuntimeError(f"r3 self-review was not gated: {gate3}")
    status = _cli(env, wt3, "status", "--task-id", task_id)
    tree3 = status["rounds"][-1]["head_tree"]
    _cli(env, wt3, "approve", "self-review", "--task-id", task_id, extra={"LABOPS_TEST_CONFIRM": tree3[:12]})
    gate3 = _cli(env, wt3, "gate", "check", "--task-id", task_id)
    if not gate3["pass"]:
        raise RuntimeError(f"r3 gate should pass after self-review acceptance: {gate3}")
    integration = _cli(env, wt3, "integrate", "propose", "--task-id", task_id)
    merged = _cli(env, wt3, "integrate", "approve", "--integration", integration["integration_id"],
                  extra={"LABOPS_TEST_CONFIRM": integration["candidate_tree"][:12]})
    if merged["state"] != "MERGED":
        raise RuntimeError(f"integration failed: {merged}")
    rounds.append(r3)

    handoff_paths = []
    for round_id in rounds:
        handoff = _cli(env, repo, "handoff", "build", "--round", round_id)
        handoff_paths.append(handoff["md_path"])
    audit = _cli(env, repo, "refs-audit", "--task-id", task_id)
    if not audit["ok"]:
        raise RuntimeError(f"refs audit found differences: {audit}")
    _cli(env, repo, "reconcile", "--repo-id", "toy")
    reconcile_again = _cli(env, repo, "reconcile", "--repo-id", "toy")
    if any(value != 0 for key, value in reconcile_again.items() if key != "ok"):
        raise RuntimeError(f"second reconcile changed state: {reconcile_again}")

    # A separate task proves the frozen-zone approval is bound to this exact SHA.
    frozen_task = "T20261019-smoke-frozen"
    _cli(env, repo, "task", "create", "--repo-id", "toy", "--task-id", frozen_task, "--goal", "Check frozen add gate", "--profile", PROFILE)
    fr = _cli(env, repo, "round", "open", "--task-id", frozen_task, "--actor", "codex", "--role", "implement")["round_id"]
    fwt = Path(_cli(env, repo, "wt", "create", "--round", fr)["path"])
    _write(fwt / "calc.py", "def add(a, b):\n    return a + b + 0\n\ndef mul(a, b):\n    if a == 0 or b == 0:\n        return 0\n    return a * b\n")
    fh = _cli(env, fwt, "snapshot", "--round", fr, "--actor", "codex", "--message", "frozen check", "--files", "calc.py")["commit"]
    _cli(env, fwt, "submit", "--round", fr, "--actor", "codex", "--commit", fh, "--request-id", "frozen")
    fv = _cli(env, fwt, "verify", "--round", fr)
    if fv["state"] != "VERIFIED":
        raise RuntimeError(f"frozen check tests failed: {fv}")
    freview = _cli(env, fwt, "review", "open", "--round", fr, "--reviewer", "opus")["review_id"]
    _cli(env, fwt, "review", "submit", "--review", freview, "--actor", "opus", "--verdict", "PASS", "--request-id", "frozen-review")
    frozen_gate = _cli(env, fwt, "gate", "check", "--task-id", frozen_task)
    if "frozen_zone_touched" not in frozen_gate["reasons"]:
        raise RuntimeError(f"frozen-zone change was not detected: {frozen_gate}")
    decision = "D-20261019-9"
    _cli(env, fwt, "approve", "frozen", "--task-id", frozen_task, "--decision", decision,
         extra={"LABOPS_TEST_CONFIRM": decision})
    after_approval = _cli(env, fwt, "gate", "check", "--task-id", frozen_task)
    if "frozen_zone_touched" in after_approval["reasons"]:
        raise RuntimeError(f"frozen approval did not bind: {after_approval}")

    task_status = _cli(env, repo, "status", "--task-id", task_id)
    main_head = _git(repo, "rev-parse", "refs/heads/main")
    summary = {"ok": True, "task_state": task_status["task"]["state"], "rounds": len(rounds),
               "main_equals_r3_head": main_head == h3, "handoffs": len(handoff_paths),
               "refs_audit_clean": audit["ok"], "reconcile_second_all_zero": True,
               "frozen_gate_blocked_then_approved": "frozen_zone_touched" in frozen_gate["reasons"] and "frozen_zone_touched" not in after_approval["reasons"]}
    print(f"smoke workspace: {root}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
