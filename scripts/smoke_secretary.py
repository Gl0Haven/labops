import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from labops import cli
from labops.config import now
from labops.db import connect, migrate
from labops.tasks import add_repo, create_task, open_round, set_confidential
from tests.helpers import make_repo

PYTHON = Path(os.environ.get("LABOPS_TEST_PYTHON") or sys.executable)


def _tool_payload(result):
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    if result.content and getattr(result.content[0], "text", None):
        text = result.content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            if start >= 0:
                try:
                    return json.loads(text[start:])
                except json.JSONDecodeError:
                    pass
            return {"text": text}
    return {}


async def _secretary_client(home: Path, task_id: str, commit: str) -> tuple[bool, bool]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update({"LABOPS_HOME": str(home), "LABOPS_ACTOR": "secretary"})
    params = StdioServerParameters(
        command=str(PYTHON), args=["-m", "labops.mcp_server"], cwd=ROOT / "src", env=env,
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {item.name for item in tools.tools}
            if not {"get_task_context", "query_history", "progress_summary"}.issubset(names):
                raise AssertionError(f"secretary read tools missing: {sorted(names)}")
            summary_result = await session.call_tool("progress_summary", {})
            if summary_result.is_error:
                raise AssertionError(_tool_payload(summary_result))
            context_result = await session.call_tool("get_task_context", {"task_id": task_id})
            if context_result.is_error:
                raise AssertionError(_tool_payload(context_result))
            context = _tool_payload(context_result)
            encoded = json.dumps(context, ensure_ascii=False)
            redacted = "SMOKE SECRET GOAL" not in encoded and str(ROOT / "private-worktree") not in encoded
            denied_result = await session.call_tool("submit_changes", {
                "round_id": f"{task_id}/r1", "commit": commit, "request_id": "secretary-write-smoke",
            })
            denied = bool(denied_result.is_error)
            if denied:
                payload = _tool_payload(denied_result)
                denied = payload.get("message") == "actor_tool_not_allowed"
            return denied, redacted


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="labops-secretary-smoke-") as temporary:
        root = Path(temporary)
        repo = root / "toy-repo"
        commit = make_repo(repo)
        home = root / "home"
        os.environ["LABOPS_HOME"] = str(home)
        conn = connect(home / "labops.db")
        try:
            migrate(conn)
            add_repo(conn, repo_id="toy", path=repo)
            task_id = "T20261020-secretary-smoke"
            create_task(conn, repo_id="toy", task_id=task_id, goal="SMOKE SECRET GOAL", profile={"schema": 1})
            set_confidential(conn, "toy", True)
            round_info = open_round(conn, task_id=task_id, actor="codex", role="implement")
            conn.execute("UPDATE rounds SET worktree_path=? WHERE round_id=?",
                         (str(ROOT / "private-worktree"), round_info["round_id"]))
            denied, redacted = asyncio.run(_secretary_client(home, task_id, commit))
            if not denied or not redacted:
                raise AssertionError({"secretary_write_denied": denied, "redacted": redacted})

            timestamp = now()
            conn.execute(
                "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
                (timestamp, "round", round_info["round_id"], "VERIFYING", "REJECTED", "test", "{}"),
            )
            conn.execute(
                "INSERT INTO integrations(integration_id,task_id,candidate_commit,candidate_tree,main_before,state,check_json,created_at,updated_at) "
                "VALUES('ig-smoke',?,?,?,?,'AWAITING_HUMAN_APPROVAL','{}',?,?)",
                (task_id, commit, "tree-smoke", commit, timestamp, timestamp),
            )
            conn.execute(
                "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
                (timestamp, "integration", "ig-smoke", "CHECKED", "AWAITING_HUMAN_APPROVAL", "test", "{}"),
            )
            conn.close()
            conn = None
            messages = []
            message_file = root / "fake-alerts.txt"

            def fake_sender(text):
                messages.append(text)
                with message_file.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(text, ensure_ascii=False) + "\n")
                return "sent"

            notify_args = cli.build_parser().parse_args(["notify", "run"])
            sent = cli.dispatch(notify_args, notify_sender=fake_sender)
            replay = cli.dispatch(notify_args, notify_sender=fake_sender)
            duplicate_alerts = replay["scanned"]
            persisted_messages = [json.loads(line) for line in
                                  message_file.read_text(encoding="utf-8").splitlines() if line]
            # Two urgent alerts in one run are merged into a single WeChat message (digest task).
            if sent["scanned"] != 2 or sent["merged_urgent"] != 2 or sent["accepted"] != 1 or \
                    replay["attempted"] != 0 or len(messages) != 1 or len(persisted_messages) != 1 or \
                    not messages[0].startswith("[LabOps · 紧急 2 条"):
                raise AssertionError({"sent": sent, "messages": len(messages), "persisted": len(persisted_messages)})
            final = {"ok": True, "secretary_write_denied": denied, "redacted": redacted,
                     "alerts_sent": len(persisted_messages), "duplicate_alerts": duplicate_alerts}
            print(json.dumps(final, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            return 0
        finally:
            if conn is not None:
                conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
