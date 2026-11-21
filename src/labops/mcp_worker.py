import json
import logging
import os
import sys
from pathlib import Path

from . import desktop, gitio, notify
from .context import task_context
from .db import connect, migrate
from .errors import LabOpsError, ValidationError
from .gate import gate_check, propose_integration as gate_propose_integration
from .handoff import build_handoff, handoff_data, render_handoff_md
from .history import query_history as history_query
from .gpuq import queue_cancel as gpuq_cancel, queue_choose as gpuq_choose, queue_intent as gpuq_intent
from .gpuq import queue_list as gpuq_list, queue_register as gpuq_register
from .next_task import next_task as next_task_impl
from .progress import progress_summary as make_progress_summary
from .redact import for_actor
from .review import open_review, submit_review as review_submit
from .runs import adopt_process as runs_adopt_process
from .search import search as search_history_impl
from .tasks import (create_worktree, open_round, round_row, snapshot as task_snapshot,
                    repo_row, submit_changes as submit_task_changes, sync_main as task_sync_main, task_row)


logger = logging.getLogger("labops.mcp_worker")
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

# This registry is intentionally data-only. Integration review compares its tool names and
# parameter names across revisions to decide whether connected clients must restart.
TOOLS = {
    "get_task_context": ("task_id", "round_id", "budget_tokens"),
    "get_handoff": ("round_id",),
    "get_worktree": ("round_id",),
    "start_work": ("task_id", "role"),
    "snapshot": ("round_id", "files", "message"),
    "sync_main": ("round_id",),
    "submit_changes": ("round_id", "commit", "request_id", "notes"),
    "submit_review": ("round_id", "verdict", "findings", "request_id"),
    "propose_integration": ("task_id",),
    "query_history": ("task_id", "sha", "limit"),
    "progress_summary": ("since_hours",),
    "search_history": ("query", "repo_id", "limit"),
    "next_task": (),
    "queue_list": (),
    "queue_intent": ("repo_id", "title", "note"),
    "queue_choose": ("queue_id", "choice"),
    "queue_register": ("repo_id", "title", "entrypoint", "context", "context_commit", "draft_id", "run_prefix"),
    "queue_cancel": ("queue_id", "reason"),
    "adopt_process": ("repo_id", "pid", "title", "log_path", "watch_globs"),
    "desktop_list": ("kind", "limit"),
    "desktop_send": ("kind", "target", "prompt", "request_id", "repo_id"),
    "desktop_status": ("request_id",),
    "desktop_dispatch": ("kind", "target", "prompt", "request_id"),
    "notify_user": ("text", "request_id"),
    "message_claude": ("target", "prompt", "request_id"),
}


def _write_log(tool: str, actor: str, round_id: str) -> None:
    logger.info("tool=%s actor=%s round=%s", tool, actor, round_id)


def _with_connection(operation):
    conn = None
    try:
        conn = connect()
        migrate(conn)
        return operation(conn)
    finally:
        if conn is not None:
            conn.close()


def _dispatch(tool_name: str, args: dict, actor: str):
    def operation(conn):
        if tool_name == "get_task_context":
            return task_context(conn, task_id=args.get("task_id"), round_id=args.get("round_id"),
                                budget_tokens=args.get("budget_tokens", 4000))
        if tool_name == "get_handoff":
            round_id = args["round_id"]
            _write_log(tool_name, actor, round_id)
            data = handoff_data(conn, round_id)
            json_path, markdown_path = build_handoff(conn, round_id)
            return {"json": data, "markdown": render_handoff_md(data),
                    "paths": [str(json_path), str(markdown_path)]}
        if tool_name == "get_worktree":
            rnd = round_row(conn, args["round_id"])
            worktree_path = rnd["worktree_path"]
            dirty_files = None
            if worktree_path and os.path.isdir(worktree_path):
                dirty_files = sorted(path.replace("\\", "/") for _, path in gitio.status_porcelain(Path(worktree_path)))
            return {"round_id": rnd["round_id"], "state": rnd["state"], "actor": rnd["actor"],
                    "branch": rnd["branch"], "worktree_path": worktree_path, "base_commit": rnd["base_commit"],
                    "head_commit": rnd["head_commit"], "dirty_files": dirty_files}
        if tool_name == "start_work":
            task_id, role = args["task_id"], args["role"]
            allowed_roles = {"implement", "fix"} if actor == "gemini" else ({"implement"} if actor == "codex" else {"fix"})
            if role not in allowed_roles:
                raise ValidationError("actor_role_not_allowed", detail={"actor": actor, "role": role,
                                                                          "allowed_role": sorted(allowed_roles)[0] if len(allowed_roles) == 1 else sorted(allowed_roles)})
            rnd = open_round(conn, task_id=task_id, actor=actor, role=role)
            _write_log(tool_name, actor, rnd["round_id"])
            worktree = create_worktree(conn, rnd["round_id"])
            result = {"round_id": rnd["round_id"], "n": rnd["n"], "base_commit": rnd["base_commit"],
                      "branch": worktree["branch"], "worktree_path": worktree["path"],
                      "worktree_status": worktree["status"]}
            if worktree["status"] != "APPLIED":
                result["status"] = worktree["status"]
                result["op_id"] = worktree["op_id"]
            if rnd.get("main_ahead"):
                result["main_ahead"] = True
                result["hint"] = rnd["hint"]
            return result
        if tool_name == "snapshot":
            _write_log(tool_name, actor, args["round_id"])
            return task_snapshot(conn, round_id=args["round_id"], files=args["files"],
                                 message=args["message"], actor=actor)
        if tool_name == "sync_main":
            _write_log(tool_name, actor, args["round_id"])
            return task_sync_main(conn, round_id=args["round_id"], actor=actor)
        if tool_name == "submit_changes":
            _write_log(tool_name, actor, args["round_id"])
            return submit_task_changes(conn, round_id=args["round_id"], actor=actor, commit=args["commit"],
                                       client_request_id=args["request_id"], notes=args.get("notes"),
                                       source="mcp")
        if tool_name == "submit_review":
            round_id, request_id = args["round_id"], args["request_id"]
            _write_log(tool_name, actor, round_id)
            old_claim = conn.execute(
                "SELECT claim_id FROM claims WHERE round_id=? AND actor=? AND kind='submit_review' "
                "AND client_request_id=?", (round_id, actor, request_id),
            ).fetchone()
            if old_claim:
                review = conn.execute("SELECT review_id FROM reviews WHERE claim_id=?", (old_claim["claim_id"],)).fetchone()
                if review is None:
                    raise ValidationError("review_claim_orphaned", detail={"claim_id": old_claim["claim_id"]})
                review_id = review["review_id"]
            else:
                waiting = conn.execute(
                    "SELECT review_id FROM reviews WHERE round_id=? AND reviewer=? AND state='WAITING' "
                    "ORDER BY created_at,review_id LIMIT 1", (round_id, actor),
                ).fetchone()
                review_id = waiting["review_id"] if waiting else open_review(conn, round_id, actor)["review_id"]
            result = review_submit(conn, review_id=review_id, actor=actor, verdict=args["verdict"],
                                   findings=args.get("findings"), client_request_id=request_id, source="mcp")
            row = conn.execute("SELECT independence FROM reviews WHERE review_id=?", (review_id,)).fetchone()
            return {"review_id": review_id, "state": result["state"],
                    "independence": row["independence"] if row else result.get("independence"),
                    "claim_id": result["claim_id"], "replayed": result.get("replayed", False)}
        if tool_name == "propose_integration":
            task_id = args["task_id"]
            latest = conn.execute("SELECT round_id FROM rounds WHERE task_id=? ORDER BY n DESC LIMIT 1", (task_id,)).fetchone()
            _write_log(tool_name, actor, latest["round_id"] if latest else "-")
            gate = gate_check(conn, task_id)
            if not gate["pass"]:
                return {"proposed": False, "reasons": gate["reasons"]}
            result = gate_propose_integration(conn, task_id)
            return {**result, "approval": f"user must run: labops integrate approve --integration {result['integration_id']} in a TTY"}
        if tool_name == "query_history":
            return history_query(conn, task_id=args.get("task_id"), sha=args.get("sha"), limit=args.get("limit", 50))
        if tool_name == "progress_summary":
            return make_progress_summary(conn, since_hours=args.get("since_hours", 24))
        if tool_name == "search_history":
            return search_history_impl(conn, query=args["query"], repo_id=args.get("repo_id"),
                                       limit=args.get("limit", 10), actor=actor)
        if tool_name == "next_task":
            return next_task_impl(conn, actor)
        if tool_name == "queue_list":
            return gpuq_list(conn)
        if tool_name == "queue_intent":
            return gpuq_intent(conn, repo_id=args["repo_id"], title=args["title"], note=args["note"],
                               created_by=actor)
        if tool_name == "queue_choose":
            return gpuq_choose(conn, queue_id=args["queue_id"], choice=args["choice"], actor=actor)
        if tool_name == "queue_register":
            return gpuq_register(conn, repo_id=args["repo_id"], title=args["title"],
                                 entrypoint=args["entrypoint"], context=args["context"],
                                 context_commit=args.get("context_commit"), draft_id=args.get("draft_id"),
                                 run_prefix=args.get("run_prefix"), created_by=actor)
        if tool_name == "queue_cancel":
            return gpuq_cancel(conn, queue_id=args["queue_id"], reason=args["reason"], actor=actor)
        if tool_name == "adopt_process":
            return runs_adopt_process(conn, repo_id=args["repo_id"], pid=args["pid"],
                                      title=args["title"], log_path=args.get("log_path"),
                                      watch_globs=args.get("watch_globs"), actor=actor)
        if tool_name == "desktop_list":
            if actor == "codex" and args["kind"] != "claude":
                raise ValidationError("kind_not_allowed")
            result = desktop.desktop_list(conn, kind=args["kind"], limit=args.get("limit", 20))
            return desktop.secretary_view(conn, result) if actor == "secretary" else result
        if tool_name == "desktop_send":
            _write_log(tool_name, actor, "-")
            return desktop.desktop_send(conn, kind=args["kind"], target=args["target"], prompt=args["prompt"],
                                        request_id=args["request_id"], repo_id=args.get("repo_id"), actor=actor)
        if tool_name == "desktop_status":
            result = desktop.desktop_status(conn, request_id=args["request_id"])
            return desktop.secretary_view(conn, result) if actor == "secretary" else result
        if tool_name == "desktop_dispatch":
            _write_log(tool_name, actor, "-")
            try:
                return desktop.desktop_dispatch(conn, kind=args["kind"], target=args["target"], prompt=args["prompt"],
                                                request_id=args["request_id"], actor=actor)
            except ValidationError as exc:
                if str(exc) == "target_ambiguous":
                    desktop.secretary_view(conn, {"sessions": exc.detail["candidates"]})
                raise
        if tool_name == "notify_user":
            return notify.notify_user(conn, actor=actor, text=args["text"], request_id=args["request_id"])
        if tool_name == "message_claude":
            _write_log(tool_name, actor, "-")
            return desktop.message_claude(conn, target=args["target"], prompt=args["prompt"],
                                          request_id=args["request_id"], actor=actor)
        raise ValidationError("unknown_tool", detail={"tool": tool_name})

    if tool_name not in TOOLS or tool_name not in _ACTOR_TOOLS[actor]:
        raise ValidationError("actor_tool_not_allowed", detail={"actor": actor, "tool": tool_name})
    return _with_connection(lambda conn: for_actor(conn, actor, operation(conn)))


def main() -> int:
    try:
        raw = sys.stdin.buffer.read()
        request = json.loads(raw.decode("utf-8"))
        actor, args = request.get("actor"), request.get("args", {})
        if actor not in _ACTOR_TOOLS:
            raise ValidationError("invalid_actor", detail={"actor": actor})
        if not isinstance(args, dict):
            raise ValidationError("invalid_tool_args")
        result = _dispatch(sys.argv[1] if len(sys.argv) > 1 else "", args, actor)
        payload = {"ok": True, "result": result}
    except LabOpsError as exc:
        payload = {"ok": False, "error": exc.code, "message": str(exc), "detail": exc.detail}
    except Exception as exc:
        logger.exception("MCP worker tool failed")
        payload = {"ok": False, "error": "internal", "message": type(exc).__name__, "detail": {}}
    output = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    sys.stdout.buffer.write(output.encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
