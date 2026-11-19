import asyncio
import json
import os
import subprocess
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from labops.db import connect, migrate
from labops.gitio import read_ref
from labops.tasks import add_repo, create_task


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"git {args!r} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _make_toy_repo(repo: Path) -> str:
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "-c", "init.defaultBranch=main", "init"],
                   capture_output=True, check=True)
    _git(repo, "config", "user.name", "labops-m0c-smoke")
    _git(repo, "config", "user.email", "labops-m0c-smoke@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "README.txt").write_text("temporary LabOps M0c smoke repository\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-m", "fixture baseline")
    return _git(repo, "rev-parse", "HEAD")


def _write_round_file(worktree: Path, name: str, content: str) -> None:
    target = worktree / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _tool_data(result):
    if result.is_error:
        message = "; ".join(item.text for item in result.content if getattr(item, "type", None) == "text")
        raise RuntimeError(f"MCP tool failed: {message}")
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    for item in result.content:
        if getattr(item, "type", None) == "text":
            return json.loads(item.text)
    return None


async def _start_client(stack: AsyncExitStack, home: Path, actor: str) -> ClientSession:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update({"LABOPS_HOME": str(home), "LABOPS_ACTOR": actor})
    params = StdioServerParameters(command=sys.executable, args=["-m", "labops.mcp_server"], cwd=SRC, env=env)
    read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
    session = ClientSession(read_stream, write_stream)
    await stack.enter_async_context(session)
    await session.initialize()
    return session


async def _run_mcp_flow(home: Path, task_id: str, repo: Path) -> dict:
    async with AsyncExitStack() as stack:
        codex = await _start_client(stack, home, "codex")
        opus = await _start_client(stack, home, "opus")
        listed = await codex.list_tools()
        names = {item.name for item in listed.tools}
        required = {"get_worktree", "start_work", "snapshot", "submit_changes", "submit_review", "propose_integration"}
        if not required.issubset(names):
            raise RuntimeError(f"MCP server is missing tools: {sorted(required - names)}")

        first = _tool_data(await codex.call_tool("start_work", {"task_id": task_id, "role": "implement"}))
        first_round = first["round_id"]
        _write_round_file(Path(first["worktree_path"]), "round1.txt", "Codex writes this file; MCP snapshots it.\n")
        first_tree = _tool_data(await codex.call_tool("get_worktree", {"round_id": first_round}))
        if first_tree["dirty_files"] != ["round1.txt"]:
            raise RuntimeError(f"unexpected dirty file list: {first_tree['dirty_files']}")
        first_snapshot = _tool_data(await codex.call_tool(
            "snapshot", {"round_id": first_round, "files": first_tree["dirty_files"], "message": "M0c round 1"}))
        _tool_data(await codex.call_tool("submit_changes", {
            "round_id": first_round, "commit": first_snapshot["commit"], "request_id": "codex-r1-snapshot"}))

        _cli_json(home, "verify", "--round", first_round)
        first_review = _tool_data(await opus.call_tool("submit_review", {
            "round_id": first_round, "verdict": "REWORK_REQUIRED", "findings": [], "request_id": "opus-r1-review"}))
        if first_review["state"] != "REWORK_REQUIRED":
            raise RuntimeError(f"unexpected first review: {first_review}")

        second = _tool_data(await codex.call_tool("start_work", {"task_id": task_id, "role": "implement"}))
        second_round = second["round_id"]
        _write_round_file(Path(second["worktree_path"]), "round2.txt", "Codex writes this file; MCP snapshots it.\n")
        second_tree = _tool_data(await codex.call_tool("get_worktree", {"round_id": second_round}))
        if second_tree["dirty_files"] != ["round2.txt"]:
            raise RuntimeError(f"unexpected dirty file list: {second_tree['dirty_files']}")
        second_snapshot = _tool_data(await codex.call_tool(
            "snapshot", {"round_id": second_round, "files": second_tree["dirty_files"], "message": "M0c round 2"}))
        _tool_data(await codex.call_tool("submit_changes", {
            "round_id": second_round, "commit": second_snapshot["commit"], "request_id": "codex-r2-snapshot"}))

        _cli_json(home, "verify", "--round", second_round)
        second_review = _tool_data(await opus.call_tool("submit_review", {
            "round_id": second_round, "verdict": "PASS", "findings": [], "request_id": "opus-r2-review"}))
        if second_review["state"] != "PASS":
            raise RuntimeError(f"unexpected second review: {second_review}")
        gate = _cli_json(home, "gate", "check", "--task-id", task_id)
        if not gate.get("pass"):
            raise RuntimeError(f"gate check failed before proposal: {gate}")
        integration = _tool_data(await opus.call_tool("propose_integration", {"task_id": task_id}))

        confirmation = integration["candidate_tree"][:12]
        _cli_json(home, "integrate", "approve", "--integration", integration["integration_id"],
                  extra_env={"LABOPS_TEST_CONFIRM": confirmation})
        _cli_json(home, "reconcile")
        second_reconcile = _cli_json(home, "reconcile")

        conn = connect(home / "labops.db")
        try:
            task_state = conn.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
            old_review_state = conn.execute("SELECT state FROM reviews WHERE review_id=?",
                                            (first_review["review_id"],)).fetchone()[0]
        finally:
            conn.close()
        main_matches = read_ref(repo, "refs/heads/main") == second_snapshot["commit"]
        all_zero = all(value == 0 for key, value in second_reconcile.items() if key != "ok")
        result = {
            "ok": bool(task_state == "INTEGRATED" and main_matches and old_review_state == "STALE" and all_zero),
            "codex_used_git_directly": False,
            "rounds": 2,
            "review_stale_after_r2": old_review_state == "STALE",
            "task_state": task_state,
            "reconcile_second_all_zero": all_zero,
        }
        return result


def _cli_json(home: Path, *args: str, extra_env: dict | None = None) -> dict:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update({"LABOPS_HOME": str(home), "LABOPS_ACTOR": "codex"})
    if extra_env:
        env.update(extra_env)
    result = subprocess.run([sys.executable, "-m", "labops.cli", *args], cwd=SRC,
                            capture_output=True, text=True, env=env, timeout=180)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"CLI returned non-JSON (exit {result.returncode}): {result.stdout}\n{result.stderr}") from exc
    if result.returncode != 0 or not payload.get("ok"):
        raise RuntimeError(f"CLI failed (exit {result.returncode}): {payload}\n{result.stderr}")
    return payload


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="labops-m0c-smoke-") as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        (home / ".test-instance").touch()
        repo = root / "toy"
        _make_toy_repo(repo)
        task_id = "T20261019-m0c-smoke"
        os.environ["LABOPS_HOME"] = str(home)
        os.environ.pop("PYTHONPATH", None)

        conn = connect(home / "labops.db")
        try:
            migrate(conn)
            add_repo(conn, repo_id="toy", path=repo, worktree_root=root / "worktrees", trusted_for_exec=True)
            profile = {
                "schema": 1,
                "commands": [{"name": "smoke", "argv": ["{python}", "-c", "print('ok')"],
                              "cwd": ".", "timeout_s": 30, "env": {}}],
                "requires_gpu": False,
                "allowed_paths": ["**"],
            }
            create_task(conn, repo_id="toy", task_id=task_id, goal="M0c dual-client MCP smoke", profile=profile)
        finally:
            conn.close()

        result = asyncio.run(_run_mcp_flow(home, task_id, repo))
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
