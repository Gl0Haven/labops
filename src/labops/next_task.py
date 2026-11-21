import sqlite3

from .context import _open_findings
from .errors import ValidationError
from .tasks import get_handoff_rules


def _trusted_tasks(conn: sqlite3.Connection, state: str | None = None):
    parameters = []
    state_filter = ""
    if state is not None:
        state_filter = " AND t.state=?"
        parameters.append(state)
    return conn.execute(
        "SELECT t.task_id,t.state,t.brief,t.created_at FROM tasks t "
        "JOIN repos p ON p.repo_id=t.repo_id "
        "WHERE (p.trusted_for_exec=1 OR p.repo_id='labops')" + state_filter +
        " ORDER BY t.created_at,t.task_id", parameters,
    ).fetchall()


def _handoff(task) -> dict:
    return {"rules": None, "brief": task["brief"]}


def _result(conn: sqlite3.Connection, action: str, task, **fields) -> dict:
    handoff = _handoff(task)
    handoff["rules"] = get_handoff_rules(conn)
    return {"action": action, **fields, "handoff": handoff, "context_ref": task["task_id"]}


def _open_round_next(conn: sqlite3.Connection, actor: str):
    row = conn.execute(
        "SELECT t.task_id,t.brief,t.created_at,r.round_id,r.worktree_path "
        "FROM rounds r JOIN tasks t ON t.task_id=r.task_id JOIN repos p ON p.repo_id=t.repo_id "
        "WHERE r.state='OPEN' AND r.actor=? AND r.worktree_path IS NOT NULL "
        "AND (p.trusted_for_exec=1 OR p.repo_id='labops') "
        "ORDER BY t.created_at,t.task_id,r.n LIMIT 1",
        (actor,),
    ).fetchone()
    if row is not None:
        return _result(conn, "continue_round", row, round_id=row["round_id"],
                       worktree_path=row["worktree_path"])
    return None


def _codex_next(conn: sqlite3.Connection, *, actor: str = "codex"):
    selected = _open_round_next(conn, actor)
    if selected is not None:
        return selected

    for task in _trusted_tasks(conn, "ACTIVE"):
        latest_round = conn.execute(
            "SELECT state FROM rounds WHERE task_id=? ORDER BY n DESC LIMIT 1", (task["task_id"],)
        ).fetchone()
        latest_review = conn.execute(
            "SELECT state,verdict FROM reviews WHERE task_id=? AND state!='STALE' "
            "ORDER BY created_at DESC,review_id DESC LIMIT 1", (task["task_id"],)
        ).fetchone()
        review_needs_rework = latest_review is not None and (
            latest_review["state"] == "REWORK_REQUIRED" or latest_review["verdict"] == "REWORK_REQUIRED"
        )
        round_rejected = latest_round is not None and latest_round["state"] == "REJECTED"
        if review_needs_rework or round_rejected:
            findings, _ = _open_findings(conn, task["task_id"])
            return _result(conn, "start_rework", task, task_id=task["task_id"], open_findings=findings)

    for task in _trusted_tasks(conn, "CREATED"):
        if conn.execute("SELECT 1 FROM rounds WHERE task_id=? LIMIT 1", (task["task_id"],)).fetchone() is None:
            return _result(conn, "start_new", task, task_id=task["task_id"])
    return None


def _opus_next(conn: sqlite3.Connection):
    row = conn.execute(
        "SELECT t.task_id,t.brief,t.created_at,r.round_id FROM rounds r "
        "JOIN tasks t ON t.task_id=r.task_id JOIN repos p ON p.repo_id=t.repo_id "
        "WHERE r.state='VERIFIED' AND t.state='ACTIVE' "
        "AND r.n=(SELECT MAX(latest.n) FROM rounds latest WHERE latest.task_id=t.task_id) "
        "AND (p.trusted_for_exec=1 OR p.repo_id='labops') "
        "AND NOT EXISTS (SELECT 1 FROM reviews v WHERE v.round_id=r.round_id AND v.state!='STALE') "
        "ORDER BY t.created_at,t.task_id,r.n LIMIT 1"
    ).fetchone()
    if row is not None:
        return _result(conn, "review", row, round_id=row["round_id"])

    row = conn.execute(
        "SELECT t.task_id,t.brief,t.created_at,r.round_id FROM rounds r "
        "JOIN tasks t ON t.task_id=r.task_id JOIN repos p ON p.repo_id=t.repo_id "
        "WHERE r.state='CLAIMED' AND t.state='ACTIVE' "
        "AND r.n=(SELECT MAX(latest.n) FROM rounds latest WHERE latest.task_id=t.task_id) "
        "AND (p.trusted_for_exec=1 OR p.repo_id='labops') "
        "ORDER BY t.created_at,t.task_id,r.n LIMIT 1"
    ).fetchone()
    if row is not None:
        return _result(conn, "verify", row, round_id=row["round_id"],
                       hint=f"labops verify --round {row['round_id']}")

    row = conn.execute(
        "SELECT t.task_id,t.brief,t.created_at,r.round_id FROM rounds r "
        "JOIN tasks t ON t.task_id=r.task_id JOIN repos p ON p.repo_id=t.repo_id "
        "WHERE r.state='BLOCKED' AND t.state='ACTIVE' "
        "AND r.n=(SELECT MAX(latest.n) FROM rounds latest WHERE latest.task_id=t.task_id) "
        "AND (p.trusted_for_exec=1 OR p.repo_id='labops') "
        "ORDER BY t.created_at,t.task_id,r.n LIMIT 1"
    ).fetchone()
    if row is not None:
        return _result(conn, "resolve_block", row, round_id=row["round_id"])
    return None


def _gemini_next(conn: sqlite3.Connection):
    selected = _open_round_next(conn, "gemini")
    if selected is not None:
        return selected
    selected = _opus_next(conn)
    if selected is not None:
        return selected
    return _codex_next(conn, actor="gemini")


def next_task(conn: sqlite3.Connection, actor: str) -> dict:
    if actor not in {"codex", "opus", "gemini"}:
        raise ValidationError("invalid_actor", detail={"actor": actor})
    if actor == "codex":
        selected = _codex_next(conn)
    elif actor == "opus":
        selected = _opus_next(conn)
    else:
        selected = _gemini_next(conn)
    return selected if selected is not None else {"action": "idle"}
