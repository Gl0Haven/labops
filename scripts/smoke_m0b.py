import asyncio
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from labops.context import task_context
from labops.db import connect, migrate
from labops.history import query_history
from labops.reconcile import reconcile
from labops.tasks import add_repo, create_task, create_worktree, open_round, submit_changes
from labops.worker import run_once


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"git {args!r} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _make_repo(repo: Path) -> str:
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "-c", "init.defaultBranch=main", "init"],
                   capture_output=True, check=True)
    _git(repo, "config", "user.name", "labops-smoke")
    _git(repo, "config", "user.email", "labops-smoke@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-m", "initial")
    return _git(repo, "rev-parse", "HEAD")


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _tool_data(result):
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    for item in result.content:
        if getattr(item, "type", None) == "text":
            return json.loads(item.text)
    return None


async def _mcp_submit(home: Path, task_id: str, round_id: str, commit: str) -> bool:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update({"LABOPS_HOME": str(home), "LABOPS_ACTOR": "codex"})
    params = StdioServerParameters(command=sys.executable, args=["-m", "labops.mcp_server"], cwd=SRC, env=env)
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {item.name for item in tools.tools}
            if not {"get_task_context", "submit_changes"}.issubset(names):
                return False
            context = await session.call_tool("get_task_context", {"task_id": task_id})
            if context.is_error or not _tool_data(context):
                return False
            submitted = await session.call_tool("submit_changes", {
                "round_id": round_id,
                "commit": commit,
                "request_id": "smoke-mcp-submit",
            })
            data = _tool_data(submitted)
            return not submitted.is_error and isinstance(data, dict) and data.get("status") == "accepted"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="labops-m0b-smoke-") as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        (home / ".test-instance").touch()
        repo = root / "toy"
        _make_repo(repo)
        task_id = "T20261019-m0b-smoke"
        round_id = f"{task_id}/r1"
        os.environ["LABOPS_HOME"] = str(home)
        os.environ["LABOPS_ACTOR"] = "codex"
        os.environ["LABOPS_FAKE_NOW"] = "2026-10-19T12:00:00Z"
        os.environ.pop("PYTHONPATH", None)

        conn = connect(home / "labops.db")
        migrate(conn)
        add_repo(conn, repo_id="toy", path=repo, worktree_root=root / "worktrees", trusted_for_exec=True)
        profile = {
            "schema": 1,
            "commands": [{"name": "smoke", "argv": ["{python}", "-c", "print('ok')"],
                          "cwd": ".", "timeout_s": 30, "env": {}}],
            "requires_gpu": False,
            "allowed_paths": ["**"],
        }
        create_task(conn, repo_id="toy", task_id=task_id, goal="M0b isolated smoke", profile=profile)
        rnd = open_round(conn, task_id=task_id, actor="codex", role="implement")
        commit = _commit_file(repo, "result.txt", "m0b\n", "m0b result")
        conn.close()

        mcp_roundtrip = asyncio.run(_mcp_submit(home, task_id, round_id, commit))
        hook_payload = json.dumps({"hook_event_name": "Stop", "session_id": "smoke-session",
                                   "turn_id": "smoke-turn", "cwd": str(repo)}).encode("utf-8")
        hook_env = os.environ.copy()
        hook_env.pop("PYTHONPATH", None)
        durations = []
        for _ in range(50):
            started = time.perf_counter()
            result = subprocess.run([sys.executable, "-m", "labops.hook", "ingest", "--client", "codex"],
                                    input=hook_payload, cwd=SRC, capture_output=True, env=hook_env, timeout=5)
            durations.append((time.perf_counter() - started) * 1000)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.decode("utf-8", "replace"))
        ordered = sorted(durations)
        hook_p50_ms = round(statistics.median(ordered), 2)
        hook_p95_ms = round(ordered[math.ceil(0.95 * len(ordered)) - 1], 2)

        conn = connect(home / "labops.db")
        migrate(conn)
        worker_result = run_once(conn)
        verified = conn.execute("SELECT state FROM rounds WHERE round_id=?", (round_id,)).fetchone()[0] == "VERIFIED"
        second_round = open_round(conn, task_id=task_id, actor="codex", role="implement")
        second_worktree = create_worktree(conn, second_round["round_id"])
        _commit_file(Path(second_worktree["path"]), "unowned.txt", "unowned\n", "unattributed change")
        first_reconcile = reconcile(conn, "toy")
        event_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE entity_id=? AND to_state='unattributed_commit'",
            (second_round["round_id"],),
        ).fetchone()[0]
        second_reconcile = reconcile(conn, "toy")
        unattributed_detected = (first_reconcile["unattributed_commits"] == 1 and event_count == 1 and
                                 second_reconcile["unattributed_commits"] == 0)

        done_dir = home / "spool" / "done"
        event_file = next(done_dir.glob("*.json"))
        replay = home / "spool" / "new" / event_file.name
        replay.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(event_file, replay)
        duplicate_result = __import__("labops.spool", fromlist=["drain"]).drain(conn)
        spool_duplicates_ignored = (duplicate_result["duplicates"] == 1 and
                                    conn.execute("SELECT COUNT(*) FROM spool_events").fetchone()[0] == 1)
        context = task_context(conn, task_id=task_id)
        history = query_history(conn, task_id=task_id)
        queried = context["sections"]["task"]["task_id"] == task_id and len(history["rounds"]) == 2
        conn.close()

        result = {
            "ok": bool(mcp_roundtrip and verified and unattributed_detected and spool_duplicates_ignored and queried and hook_p95_ms < 400),
            "mcp_roundtrip": mcp_roundtrip,
            "worker_verified": verified,
            "unattributed_detected": unattributed_detected,
            "spool_duplicates_ignored": spool_duplicates_ignored,
            "context_history_queried": queried,
            "hook_ingest_p50_ms": hook_p50_ms,
            "hook_ingest_p95_ms": hook_p95_ms,
            "worker_reconciled_repos": len(worker_result["reconciled"]),
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
