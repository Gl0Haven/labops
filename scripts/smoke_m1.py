import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))

from labops import mcp_server
from labops.backup import run_backup, verify_restore
from labops.db import connect, migrate
from labops.legacy import audit_legacy, import_legacy
from labops.tasks import (abandon_task, add_repo, create_task, create_worktree, open_round,
                          prune_worktrees, set_repo_options, submit_changes)
from labops.verify import verify_round
from tests.helpers import commit_file, make_repo


def _tree_state(root: Path):
    return {path.relative_to(root).as_posix(): (path.stat().st_mtime_ns, path.read_bytes())
            for path in root.rglob("*") if path.is_file()}


def main() -> int:
    checks = {
        "ok": True,
        "mcp_hot_reload": False,
        "per_repo_python_used": False,
        "backup_verified": False,
        "restore_refs_match": False,
        "legacy_imported": False,
        "legacy_readonly": False,
        "drift_detected": False,
        "prune_dry_run_safe": False,
    }
    with tempfile.TemporaryDirectory(prefix="labops-smoke-m1-") as temporary:
        root = Path(temporary)
        home = root / "home"
        repo = root / "toy-repo"
        base = make_repo(repo, files={"README.txt": "toy\n", "tracked.txt": "base\n"})
        os.environ["LABOPS_HOME"] = str(home)
        os.environ["LABOPS_ACTOR"] = "codex"
        conn = connect(home / "labops.db")
        try:
            migrate(conn)
            add_repo(conn, repo_id="toy", path=repo, trusted_for_exec=True,
                     worktree_root=root / "worktrees")
            profile = {"schema": 1,
                       "commands": [{"name": "python", "argv": ["{python}", "-c", "import sys; print(sys.executable)"],
                                     "cwd": ".", "timeout_s": 30}],
                       "requires_gpu": False, "allowed_paths": ["**/*"], "max_log_bytes": 2048}
            task_id = "T20261020-m1-smoke"
            create_task(conn, repo_id="toy", task_id=task_id, goal="M1 smoke", profile=profile, base=base)
            set_repo_options(conn, "toy", python=sys.executable, backup_root=str(root / "backups"),
                             backup_keep=2, legacy_bridge=".ai-bridge")
            rnd = open_round(conn, task_id=task_id, actor="codex", role="implement")
            submitted = commit_file(repo, "result.txt", "verified\n", "smoke verification commit")
            submit_changes(conn, round_id=rnd["round_id"], actor="codex", commit=submitted,
                           client_request_id="smoke-m1-submit")
            verified = verify_round(conn, rnd["round_id"])
            env_info = json.loads(conn.execute("SELECT env_json FROM evidence WHERE evidence_id=?",
                                                (verified["evidence_id"],)).fetchone()[0])
            log_text = Path(verified["results"][0]["log_path"]).read_text(encoding="utf-8")
            checks["per_repo_python_used"] = (env_info["python_resolved"] == str(Path(sys.executable).resolve())
                                              and str(Path(sys.executable).resolve()) in log_text)

            import subprocess
            git = shutil.which("git")
            subprocess.run([git, "-C", str(repo), "update-ref", "refs/labops/m1-smoke/submitted", submitted],
                           check=True, capture_output=True)
            bundle = run_backup(conn, "toy")
            restored = verify_restore(conn, "toy")
            checks["backup_verified"] = bundle["verified"] is True
            checks["restore_refs_match"] = restored["ok"] is True

            bridge = repo / ".ai-bridge"
            review = bridge / "review" / "T20261020-m1-legacy.md"
            review.parent.mkdir(parents=True)
            (bridge / "agent-status.md").write_text(
                "owner: opus\nstate: review\ntask: T20261020-m1-legacy\nround: 1\n"
                "updated: 2020-01-01T00:00:00Z\nbaseline: " + base + "\nspec: private/spec.md\n\n# body\n",
                encoding="utf-8")
            (bridge / "decisions.md").write_text("## D-20261020-1 smoke decision\nbody\n", encoding="utf-8")
            review.write_text("# smoke review title\nprivate body\n", encoding="utf-8")
            before = _tree_state(bridge)
            imported = import_legacy(conn, "toy")
            checks["legacy_imported"] = imported["imported"] >= 9
            checks["legacy_readonly"] = _tree_state(bridge) == before
            commit_file(repo, "advanced.txt", "advanced\n", "advance beyond legacy baseline")
            (repo / "README.txt").write_text("dirty\n", encoding="utf-8")
            drift = audit_legacy(conn, "toy")
            checks["drift_detected"] = {item["kind"] for item in drift["detected"]} >= {
                "behind", "dirty_worktree", "stale_updated"}

            prune_task = "T20261020-m1-prune"
            create_task(conn, repo_id="toy", task_id=prune_task, goal="prune smoke", profile={"schema": 1})
            prune_round = open_round(conn, task_id=prune_task, actor="codex", role="implement")
            worktree = create_worktree(conn, prune_round["round_id"])
            abandon_task(conn, prune_task)
            preview = prune_worktrees(conn, prune_task)
            checks["prune_dry_run_safe"] = (Path(worktree["path"]).is_dir()
                                              and preview["worktrees"][0]["status"] == "would_remove")

            code_root = root / "reload-src"
            shutil.copytree(SRC / "labops", code_root / "labops",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            os.environ["LABOPS_CODE_ROOT"] = str(code_root)
            first = mcp_server._call_worker("get_task_context",
                                            {"task_id": task_id, "round_id": None, "budget_tokens": 4000}, "codex")
            worker = code_root / "labops" / "mcp_worker.py"
            source = worker.read_text(encoding="utf-8")
            old = (
                "return task_context(conn, task_id=args.get(\"task_id\"), round_id=args.get(\"round_id\"),\n"
                "                                budget_tokens=args.get(\"budget_tokens\", 4000))"
            )
            replacement = "return {\"reloaded\": Path(os.environ[\"LABOPS_TEST_ECHO\"]).read_text(encoding=\"utf-8\")}"
            if source.count(old) != 1:
                raise RuntimeError("worker source hook not found exactly once")
            worker.write_text(source.replace(old, replacement), encoding="utf-8")
            echo_path = root / "echo.txt"
            echo_path.write_text("new worker implementation", encoding="utf-8")
            os.environ["LABOPS_TEST_ECHO"] = str(echo_path)
            second = mcp_server._call_worker("get_task_context",
                                             {"task_id": task_id, "round_id": None, "budget_tokens": 4000}, "codex")
            checks["mcp_hot_reload"] = (first["sections"]["task"]["task_id"] == task_id
                                        and second == {"reloaded": "new worker implementation"})
            checks["ok"] = all(value for key, value in checks.items() if key != "ok")
        finally:
            conn.close()
    print(json.dumps(checks, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if checks["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
