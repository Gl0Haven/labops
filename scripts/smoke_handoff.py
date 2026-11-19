import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))

from labops import mcp_server
from labops.config import now
from labops.db import connect, migrate
from labops.gate import client_restart_requirement
from labops.gitio import tree_of
from labops.index import rebuild_index
from labops.next_task import next_task
from labops.notify import list_notifications, mute_kind, scan, send_pending
from labops.search import search
from labops.tasks import add_repo, create_task, create_worktree, open_round
from tests.helpers import commit_file, make_repo


def _event(conn, entity_type: str, entity_id: str, state: str) -> None:
    conn.execute(
        "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) "
        "VALUES(?,?,?,?,?,?,?)", (now(), entity_type, entity_id, None, state, "smoke", "{}"),
    )


def _restart_flag(root: Path) -> bool:
    repo = root / "restart-toy"
    base = make_repo(repo, files={
        "src/labops/mcp_server.py": "def search_history(query):\n    return query\n",
        "src/labops/mcp_worker.py": "TOOLS = {'search_history': ('query',)}\n",
    })
    candidate = commit_file(
        repo, "src/labops/mcp_server.py",
        "def search_history(query):\n    return query\n\ndef next_task():\n    return {}\n",
        "add next_task MCP tool",
    )
    return client_restart_requirement(repo, base, candidate) == {
        "client_restart_required": True, "reason": "mcp_server_changed",
    }


def main() -> int:
    checks = {
        "ok": True,
        "utf8_gbk_roundtrip": False,
        "next_task_codex_flow": False,
        "next_task_opus_flow": False,
        "brief_in_context": False,
        "completion_alerts": 0,
        "muted_kind_suppressed": False,
        "review_section_hit": False,
        "restart_flag": False,
    }
    with tempfile.TemporaryDirectory(prefix="labops-smoke-handoff-") as temporary:
        root = Path(temporary)
        home = root / "home"
        repo = root / "toy-repo"
        base = make_repo(repo, files={"README.txt": "temporary handoff repository\n"})
        review_dir = repo / ".ai-bridge" / "review"
        review_dir.mkdir(parents=True)
        (review_dir / "review.md").write_text(
            "# Review\n## Handoff index\nreviewsectionneedle is searchable\n", encoding="utf-8")
        os.environ.update({"LABOPS_HOME": str(home), "LABOPS_CODE_ROOT": str(SRC), "LABOPS_ACTOR": "codex"})

        conn = connect(home / "labops.db")
        try:
            migrate(conn)
            add_repo(conn, repo_id="toy", path=repo, worktree_root=root / "worktrees", trusted_for_exec=True)
            task_id = "T20261020-handoff-smoke"
            brief = "Smoke brief: inspect handoff and UTF-8 behavior."
            create_task(conn, repo_id="toy", task_id=task_id,
                        goal="中文编码查询 should survive a GBK stdio environment",
                        profile={"schema": 1}, brief=brief, base=base)
            with patch.dict(os.environ, {"LABOPS_ACTOR": "codex"}):
                start = mcp_server.next_task()
            rnd = open_round(conn, task_id=task_id, actor="codex", role="implement")
            worktree = create_worktree(conn, rnd["round_id"])
            continuing = next_task(conn, "codex")
            checks["next_task_codex_flow"] = (
                start["action"] == "start_new" and start["task_id"] == task_id
                and continuing["action"] == "continue_round"
                and continuing["worktree_path"] == worktree["path"]
            )

            context = mcp_server.get_task_context(task_id=task_id)
            checks["brief_in_context"] = context["sections"]["handoff"]["brief"] == brief

            docs = rebuild_index(conn, "toy")
            review_hit = search(conn, "reviewsectionneedle", repo_id="toy", kinds=["review_section"])
            checks["review_section_hit"] = (
                bool(review_hit["results"]) and review_hit["results"][0]["ref"] == "review.md#1"
                and review_hit["results"][0]["line"] == 2
            )

            tree = tree_of(repo, base)
            conn.execute("UPDATE rounds SET state='CLAIMED' WHERE round_id=?", (rnd["round_id"],))
            with patch.dict(os.environ, {"LABOPS_ACTOR": "opus"}):
                verify = mcp_server.next_task()
            conn.execute("UPDATE rounds SET state='VERIFIED',head_commit=?,head_tree=? WHERE round_id=?",
                          (base, tree, rnd["round_id"]))
            with patch.dict(os.environ, {"LABOPS_ACTOR": "opus"}):
                review_action = mcp_server.next_task()
            checks["next_task_opus_flow"] = (
                verify["action"] == "verify" and verify["hint"] == f"labops verify --round {rnd['round_id']}"
                and review_action["action"] == "review" and review_action["round_id"] == rnd["round_id"]
            )

            # Seed the four completion events to exercise notify's public outbox path.
            _event(conn, "round", rnd["round_id"], "CLAIMED")
            _event(conn, "round", rnd["round_id"], "VERIFIED")
            timestamp = now()
            profile_hash = conn.execute("SELECT profile_hash FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO reviews(review_id,task_id,round_id,reviewer,state,verdict,independence,base_commit,reviewed_commit,reviewed_tree,scope_id,acceptance_profile_hash,findings_json,created_at,updated_at) "
                "VALUES('rv-smoke-pass',?,?,'opus','PASS','PASS','INDEPENDENT',?,?,?,'scope',?,'[]',?,?)",
                (task_id, rnd["round_id"], base, base, tree, profile_hash, timestamp, timestamp),
            )
            _event(conn, "review", "rv-smoke-pass", "PASS")
            conn.execute(
                "INSERT INTO integrations(integration_id,task_id,candidate_commit,candidate_tree,main_before,state,check_json,created_at,updated_at) "
                "VALUES('ig-smoke-merged',?,?,?,?, 'MERGED','{}',?,?)",
                (task_id, base, tree, base, timestamp, timestamp),
            )
            _event(conn, "integration", "ig-smoke-merged", "MERGED")
            scan(conn)
            expected_kinds = {"submitted", "verified", "review_passed", "merged"}
            completion_kinds = {item["kind"] for item in list_notifications(conn)} & expected_kinds
            checks["completion_alerts"] = len(completion_kinds)
            send_pending(conn, lambda _text: "sent")

            mute_kind(conn, "submitted")
            _event(conn, "round", rnd["round_id"], "CLAIMED")
            inserted = scan(conn)
            pending_send = send_pending(conn, lambda _text: "sent")
            checks["muted_kind_suppressed"] = inserted == 0 and pending_send["attempted"] == 0
        finally:
            conn.close()

        with patch.dict(os.environ, {"LABOPS_ACTOR": "opus", "PYTHONIOENCODING": "gbk"}):
            query = mcp_server.search_history(query="中文编码", repo_id="toy")
            query_ok = any("中文编码" in item.get("snippet", "") for item in query["results"])
            submitted = mcp_server.submit_review(
                round_id=f"T20261020-handoff-smoke/r1", verdict="REWORK_REQUIRED",
                findings=[{"id": "F-GBK", "severity": "major", "path": "README.txt",
                           "text": "中文 findings 应原样保存"}], request_id="smoke-gbk",
            )
            context = mcp_server.get_task_context(task_id="T20261020-handoff-smoke")
            finding_ok = (submitted["state"] == "REWORK_REQUIRED"
                          and context["sections"]["open_findings"]["findings"][0]["text"] == "中文 findings 应原样保存")
            checks["utf8_gbk_roundtrip"] = query_ok and finding_ok

        checks["restart_flag"] = _restart_flag(root)
        checks["ok"] = (
            checks["utf8_gbk_roundtrip"] and checks["next_task_codex_flow"]
            and checks["next_task_opus_flow"] and checks["brief_in_context"]
            and checks["completion_alerts"] == 4 and checks["muted_kind_suppressed"]
            and checks["review_section_hit"] and checks["restart_flag"]
        )
    print(json.dumps(checks, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if checks["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
