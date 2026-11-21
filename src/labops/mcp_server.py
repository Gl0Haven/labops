import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
logger = logging.getLogger("labops.mcp")
logger.setLevel(logging.INFO)
server = MCPServer(name="labops", version="0.1.0")

_ACTOR_TOOLS = {
    "codex": {"get_task_context", "get_handoff", "get_worktree", "start_work", "snapshot", "sync_main",
              "submit_changes", "submit_review", "propose_integration", "query_history", "search_history", "next_task",
              "queue_list", "queue_register", "queue_cancel", "adopt_process", "notify_user",
              "desktop_list", "message_claude"},
    "opus": {"get_task_context", "get_handoff", "get_worktree", "start_work", "snapshot", "sync_main",
             "submit_changes", "submit_review", "propose_integration", "query_history", "progress_summary", "search_history", "next_task",
             "queue_list", "queue_register", "queue_cancel", "adopt_process",
             "desktop_list", "desktop_send", "desktop_status", "notify_user"},
    "gemini": {"get_task_context", "get_handoff", "get_worktree", "start_work", "snapshot", "sync_main",
               "submit_changes", "submit_review", "propose_integration", "query_history", "progress_summary", "search_history", "next_task",
               "queue_list", "queue_intent", "queue_register", "queue_cancel", "adopt_process",
               "desktop_list", "desktop_send", "desktop_status", "notify_user"},
    "secretary": {"get_task_context", "query_history", "progress_summary", "search_history",
                  "queue_list", "queue_intent", "queue_choose", "desktop_list", "desktop_status",
                  "desktop_dispatch"},
}


def _actor() -> str:
    actor = os.environ.get("LABOPS_ACTOR")
    if actor not in _ACTOR_TOOLS:
        raise ToolError(json.dumps({"error": "invalid_actor", "message": "invalid_actor",
                                    "detail": {"actor": actor}}, ensure_ascii=False,
                                   sort_keys=True, separators=(",", ":")))
    return actor


def _failure(stderr: str, *, tool_name: str, reason: str) -> ToolError:
    lines = stderr.splitlines()[-20:]
    logger.error("worker_failed tool=%s reason=%s stderr_tail=%s", tool_name, reason, "\\n".join(lines))
    return ToolError('{"error":"worker_failed"}')


def _call_worker(tool_name: str, args: dict, actor: str):
    if tool_name not in _ACTOR_TOOLS[actor]:
        payload = {"error": "actor_tool_not_allowed", "message": "actor_tool_not_allowed",
                   "detail": {"actor": actor, "tool": tool_name}}
        raise ToolError(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    code_root = os.environ.get("LABOPS_CODE_ROOT") or str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    env["LABOPS_ACTOR"] = actor
    env["PYTHONPATH"] = code_root
    request = json.dumps({"actor": actor, "args": args}, ensure_ascii=False, separators=(",", ":"))
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "labops.mcp_worker", tool_name], input=request,
            capture_output=True, timeout=55, env=env, text=True, encoding="utf-8", errors="replace", shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        raise _failure(stderr, tool_name=tool_name, reason="timeout") from exc
    except OSError as exc:
        raise _failure(str(exc), tool_name=tool_name, reason=type(exc).__name__) from exc
    if completed.returncode != 0:
        raise _failure(completed.stderr, tool_name=tool_name, reason=f"exit_{completed.returncode}")
    lines = completed.stdout.splitlines()
    if len(lines) != 1:
        raise _failure(completed.stderr, tool_name=tool_name, reason="invalid_stdout")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise _failure(completed.stderr, tool_name=tool_name, reason="invalid_json") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise _failure(completed.stderr, tool_name=tool_name, reason="invalid_payload")
    if payload["ok"]:
        return payload.get("result")
    error_payload = {"error": payload.get("error", "internal"),
                     "message": payload.get("message", "internal"),
                     "detail": payload.get("detail", {})}
    raise ToolError(json.dumps(error_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _invoke(tool_name: str, **kwargs):
    actor = _actor()
    return _call_worker(tool_name, kwargs, actor)


@server.tool()
def get_task_context(task_id: str | None = None, round_id: str | None = None,
                     budget_tokens: int = 4000) -> dict:
    return _invoke("get_task_context", task_id=task_id, round_id=round_id, budget_tokens=budget_tokens)


@server.tool()
def get_handoff(round_id: str) -> dict:
    return _invoke("get_handoff", round_id=round_id)


@server.tool()
def get_worktree(round_id: str) -> dict:
    return _invoke("get_worktree", round_id=round_id)


@server.tool()
def start_work(task_id: str, role: str) -> dict:
    return _invoke("start_work", task_id=task_id, role=role)


@server.tool()
def snapshot(round_id: str, files: list[str], message: str) -> dict:
    return _invoke("snapshot", round_id=round_id, files=files, message=message)


@server.tool()
def sync_main(round_id: str) -> dict:
    return _invoke("sync_main", round_id=round_id)


@server.tool()
def submit_changes(round_id: str, commit: str, request_id: str, notes: dict | None = None) -> dict:
    return _invoke("submit_changes", round_id=round_id, commit=commit, request_id=request_id, notes=notes)


@server.tool()
def submit_review(round_id: str, verdict: str, findings: list[dict] | None,
                  request_id: str) -> dict:
    return _invoke("submit_review", round_id=round_id, verdict=verdict, findings=findings, request_id=request_id)


@server.tool()
def propose_integration(task_id: str) -> dict:
    return _invoke("propose_integration", task_id=task_id)


@server.tool()
def query_history(task_id: str | None = None, sha: str | None = None, limit: int = 50) -> dict:
    return _invoke("query_history", task_id=task_id, sha=sha, limit=limit)


@server.tool()
def search_history(query: str, repo_id: str | None = None, limit: int = 10) -> dict:
    return _invoke("search_history", query=query, repo_id=repo_id, limit=limit)


@server.tool()
def progress_summary(since_hours: int = 24) -> dict:
    return _invoke("progress_summary", since_hours=since_hours)


@server.tool()
def next_task() -> dict:
    return _invoke("next_task")


@server.tool()
def queue_list() -> dict:
    return _invoke("queue_list")


@server.tool()
def queue_intent(repo_id: str, title: str, note: str) -> dict:
    return _invoke("queue_intent", repo_id=repo_id, title=title, note=note)


@server.tool()
def queue_choose(queue_id: str, choice: str) -> dict:
    return _invoke("queue_choose", queue_id=queue_id, choice=choice)


@server.tool()
def queue_register(repo_id: str, title: str, entrypoint: str, context: str,
                   context_commit: str | None = None, draft_id: str | None = None,
                   run_prefix: str | None = None) -> dict:
    return _invoke("queue_register", repo_id=repo_id, title=title, entrypoint=entrypoint,
                   context=context, context_commit=context_commit, draft_id=draft_id,
                   run_prefix=run_prefix)


@server.tool()
def queue_cancel(queue_id: str, reason: str) -> dict:
    return _invoke("queue_cancel", queue_id=queue_id, reason=reason)


@server.tool()
def adopt_process(repo_id: str, pid: int, title: str, log_path: str | None = None,
                  watch_globs: list[str] | None = None) -> dict:
    return _invoke("adopt_process", repo_id=repo_id, pid=pid, title=title,
                   log_path=log_path, watch_globs=watch_globs)


@server.tool()
def desktop_list(kind: str, limit: int = 20) -> dict:
    return _invoke("desktop_list", kind=kind, limit=limit)


@server.tool()
def desktop_send(kind: str, target: str, prompt: str, request_id: str, repo_id: str | None = None) -> dict:
    return _invoke("desktop_send", kind=kind, target=target, prompt=prompt, request_id=request_id, repo_id=repo_id)


@server.tool()
def desktop_status(request_id: str) -> dict:
    return _invoke("desktop_status", request_id=request_id)


@server.tool()
def desktop_dispatch(kind: str, target: str, prompt: str, request_id: str) -> dict:
    return _invoke("desktop_dispatch", kind=kind, target=target, prompt=prompt, request_id=request_id)


@server.tool()
def notify_user(text: str, request_id: str) -> dict:
    return _invoke("notify_user", text=text, request_id=request_id)


@server.tool()
def message_claude(target: str, prompt: str, request_id: str) -> dict:
    return _invoke("message_claude", target=target, prompt=prompt, request_id=request_id)


def main() -> int:
    try:
        _actor()
    except ToolError:
        logger.error("MCP startup rejected: invalid actor")
        return 2
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
