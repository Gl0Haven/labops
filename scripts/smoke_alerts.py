import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))

from labops.config import now
from labops.db import connect, migrate
from labops.gitio import tree_of
from labops.notify import list_notifications, scan, set_alert_detail
from labops.tasks import add_repo, create_task, open_round
from tests.helpers import commit_file, make_repo


def _event(conn, entity_type: str, entity_id: str, state: str, detail: dict | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) "
        "VALUES(?,?,?,?,?,?,?)",
        (now(), entity_type, entity_id, None, state, "smoke", json.dumps(detail or {}, ensure_ascii=False)),
    )
    return cursor.lastrowid


def _review(conn, *, review_id: str, task_id: str, round_id: str, reviewer: str,
            state: str, verdict: str, profile_hash: str, commit: str, tree: str,
            findings: list[dict] | None = None) -> None:
    timestamp = now()
    conn.execute(
        "INSERT INTO reviews(review_id,task_id,round_id,reviewer,state,verdict,independence,base_commit,reviewed_commit,reviewed_tree,scope_id,acceptance_profile_hash,findings_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?, 'INDEPENDENT',?,?,?,?,?,?,?,?)",
        (review_id, task_id, round_id, reviewer, state, verdict, commit, commit, tree, "scope",
         profile_hash, json.dumps(findings or [], ensure_ascii=False), timestamp, timestamp),
    )


def _integration(conn, *, integration_id: str, task_id: str, state: str,
                 commit: str, tree: str) -> None:
    timestamp = now()
    conn.execute(
        "INSERT INTO integrations(integration_id,task_id,candidate_commit,candidate_tree,main_before,state,check_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?, '{}',?,?)",
        (integration_id, task_id, commit, tree, commit, state, timestamp, timestamp),
    )


def main() -> int:
    checks = {
        "ok": True,
        "kinds_covered": 0,
        "max_len_ok": False,
        "detail_off_first_line_only": False,
        "no_llm_calls": False,
    }
    expected_kinds = {
        "submitted", "verified", "verify_failed", "verify_blocked", "review_blocking",
        "review_passed", "awaiting_approval", "merged", "integrity", "legacy_drift",
    }
    with tempfile.TemporaryDirectory(prefix="labops-smoke-alerts-") as temporary:
        root = Path(temporary)
        home = root / "home"
        repo = root / "toy-repo"
        base = make_repo(repo, files={"README.txt": "temporary LabOps alert fixture\n"})
        os.environ["LABOPS_HOME"] = str(home)

        conn = connect(home / "labops.db")
        try:
            migrate(conn)
            add_repo(conn, repo_id="toy", path=repo)
            task_id = "T20261020-labops-alerts-smoke"
            task = create_task(conn, repo_id="toy", task_id=task_id, goal="toy alerts only",
                               profile={"schema": 1}, base=base)
            round_id = open_round(conn, task_id=task_id, actor="codex", role="implement")["round_id"]
            head = commit_file(repo, "change.txt", "toy change\n")
            tree = tree_of(repo, head)
            conn.execute("UPDATE rounds SET head_commit=?,head_tree=? WHERE round_id=?",
                         (head, tree, round_id))

            # Submit and verification events use the matching evidence available at scan time.
            _event(conn, "round", round_id, "CLAIMED")
            conn.execute(
                "INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,results_json,producer,created_at,finished_at) "
                "VALUES('ev-alert-pass',?,'INDEPENDENTLY_EXECUTED','passed',?,?,?,?, 'smoke',?,?)",
                (round_id, head, tree, task["profile_hash"],
                 json.dumps([{"name": "unit", "exit_code": 0, "duration_ms": 250, "timed_out": False}]),
                 now(), now()),
            )
            _event(conn, "round", round_id, "VERIFIED")
            scan(conn)

            conn.execute(
                "INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,results_json,producer,created_at,finished_at) "
                "VALUES('ev-alert-fail',?,'INDEPENDENTLY_EXECUTED','failed',?,?,?,?, 'smoke',?,?)",
                (round_id, head, tree, task["profile_hash"],
                 json.dumps([{"name": "unit", "exit_code": 1, "duration_ms": 350, "timed_out": False}]),
                 now(), now()),
            )
            _event(conn, "round", round_id, "REJECTED")
            scan(conn)
            _event(conn, "round", round_id, "BLOCKED", {"reason": "repo_not_trusted_for_exec"})
            scan(conn)

            _review(conn, review_id="rv-alert-blocking", task_id=task_id, round_id=round_id,
                    reviewer="opus", state="REWORK_REQUIRED", verdict="REWORK_REQUIRED",
                    profile_hash=task["profile_hash"], commit=head, tree=tree,
                    findings=[{"id": "HO-1", "severity": "major", "text": "old verified round is stale"}])
            _event(conn, "review", "rv-alert-blocking", "REWORK_REQUIRED")
            scan(conn)

            _review(conn, review_id="rv-alert-pass", task_id=task_id, round_id=round_id,
                    reviewer="opus", state="PASS_WITH_NOTES", verdict="PASS_WITH_NOTES",
                    profile_hash=task["profile_hash"], commit=head, tree=tree,
                    findings=[{"id": "N-1", "severity": "note", "text": "toy note"}])
            _event(conn, "review", "rv-alert-pass", "PASS_WITH_NOTES")
            scan(conn)

            _integration(conn, integration_id="ig-alert-approval", task_id=task_id,
                         state="AWAITING_HUMAN_APPROVAL", commit=head, tree=tree)
            _event(conn, "integration", "ig-alert-approval", "AWAITING_HUMAN_APPROVAL")
            scan(conn)
            _integration(conn, integration_id="ig-alert-merged", task_id=task_id,
                         state="MERGED", commit=head, tree=tree)
            _event(conn, "integration", "ig-alert-merged", "MERGED")
            scan(conn)

            _event(conn, "round", round_id, "integrity_conflict", {"ref": "refs/labops/toy/r1/codex/submitted"})
            scan(conn)
            _event(conn, "repo", "toy", "legacy_drift", {
                "kind": "baseline_diverged", "baseline": base, "head": head,
            })
            scan(conn)

            rows = list_notifications(conn)
            kinds = {row["kind"] for row in rows}
            checks["kinds_covered"] = len(expected_kinds & kinds)
            checks["max_len_ok"] = all(len(row["text"]) <= 140 for row in rows)

            set_alert_detail(conn, "toy", False)
            seq = _event(conn, "round", round_id, "BLOCKED", {"reason": "details disabled"})
            scan(conn)
            short = conn.execute("SELECT text FROM notifications WHERE event_key=?", (f"ev:{seq}",)).fetchone()
            checks["detail_off_first_line_only"] = bool(short and short["text"] ==
                "[LabOps · toy] alerts-smoke r1 · LabOps 验证受阻" and "\n" not in short["text"])
        finally:
            conn.close()

        checks["no_llm_calls"] = not any(name.startswith("labops.llm") for name in sys.modules)
        checks["ok"] = (
            checks["kinds_covered"] == 10 and checks["max_len_ok"]
            and checks["detail_off_first_line_only"] and checks["no_llm_calls"]
        )
    print(json.dumps(checks, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if checks["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
