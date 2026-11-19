import os
import re
import sqlite3
import sys

from . import gitio
from .canon import canonical_json
from .config import is_test_instance, now
from .db import tx
from .errors import Blocked, ValidationError
from .tasks import repo_row, task_row

_DECISION_RE = re.compile(r"^D-\d{8}-\d+$")


def require_human(prompt: str, expect: str) -> str:
    if sys.stdin.isatty() and sys.stdout.isatty():
        print(prompt, file=sys.stderr, flush=True)
        answer = sys.stdin.readline().rstrip("\r\n")
        if answer != expect:
            raise Blocked("human_confirmation_mismatch", detail={"expected": expect})
        return "tty"
    if is_test_instance() and os.environ.get("LABOPS_TEST_CONFIRM") == expect:
        return "test-instance"
    raise Blocked("human_confirmation_required", detail={"expect": expect})


def _latest_verified(conn: sqlite3.Connection, task_id: str):
    task = task_row(conn, task_id)
    rnd = conn.execute("SELECT * FROM rounds WHERE task_id=? AND head_commit IS NOT NULL ORDER BY n DESC LIMIT 1", (task_id,)).fetchone()
    if rnd is None or rnd["state"] != "VERIFIED":
        raise Blocked("round_not_verified", detail={"task_id": task_id, "state": rnd["state"] if rnd else None})
    return task, rnd


def _insert_approval(conn: sqlite3.Connection, *, task_id: str, kind: str, commit: str, tree: str,
                     granted_via: str, decision_id: str | None = None, integration_id: str | None = None) -> dict:
    from .ids import new_id
    approval_id = new_id("ap")
    timestamp = now()
    conn.execute("INSERT INTO approvals(approval_id,task_id,kind,target_commit,target_tree,decision_id,integration_id,granted_via,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (approval_id, task_id, kind, commit, tree, decision_id, integration_id, granted_via, timestamp))
    conn.execute("INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
                 (timestamp, "approval", approval_id, None, "GRANTED", "user",
                  canonical_json({"task_id": task_id, "kind": kind, "target_commit": commit, "target_tree": tree,
                                  "decision_id": decision_id, "granted_via": granted_via}).decode()))
    return {"approval_id": approval_id, "kind": kind, "target_commit": commit, "target_tree": tree,
            "decision_id": decision_id, "granted_via": granted_via}


def approve_self_review(conn: sqlite3.Connection, task_id: str) -> dict:
    task, rnd = _latest_verified(conn, task_id)
    review = conn.execute("SELECT review_id FROM reviews WHERE round_id=? AND state IN ('PASS','PASS_WITH_NOTES') "
                          "AND independence='SELF_REVIEWED_FIX' AND reviewed_tree=? AND acceptance_profile_hash=? "
                          "ORDER BY created_at DESC LIMIT 1",
                          (rnd["round_id"], rnd["head_tree"], task["profile_hash"])).fetchone()
    if review is None:
        raise Blocked("self_review_not_found", detail={"round_id": rnd["round_id"]})
    grant = require_human(f"Accept self-reviewed fix for tree {rnd['head_tree'][:12]}? Type the tree prefix:", rnd["head_tree"][:12])
    with tx(conn):
        return _insert_approval(conn, task_id=task_id, kind="accept_self_review", commit=rnd["head_commit"],
                                tree=rnd["head_tree"], granted_via=grant)


def approve_frozen(conn: sqlite3.Connection, task_id: str, decision_id: str) -> dict:
    if not isinstance(decision_id, str) or not _DECISION_RE.fullmatch(decision_id):
        raise ValidationError("invalid_decision_id", detail={"decision_id": decision_id})
    task, rnd = _latest_verified(conn, task_id)
    grant = require_human(f"Approve frozen-zone change {decision_id}? Type the decision ID:", decision_id)
    with tx(conn):
        return _insert_approval(conn, task_id=task_id, kind="frozen_change", commit=rnd["head_commit"],
                                tree=rnd["head_tree"], decision_id=decision_id, granted_via=grant)
