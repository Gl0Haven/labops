import sqlite3
from datetime import datetime, timedelta, timezone

from .config import now
from .context import _next_action
from .errors import ValidationError
from .gate import evaluate
from .gpuq import queue_progress


def _short(value: str | None) -> str | None:
    return value[:12] if isinstance(value, str) and value else None


def _alerts_paused(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM meta WHERE key='wechat_paused_at'").fetchone()
    if row is None or not row["value"]:
        return None
    return {"since": row["value"], "hint": "微信凭证失效：请给 bot 发一句话"}


def progress_summary(conn: sqlite3.Connection, since_hours: int = 24) -> dict:
    if isinstance(since_hours, bool) or not isinstance(since_hours, int) or not 1 <= since_hours <= 168:
        raise ValidationError("invalid_progress_window", detail={"since_hours": since_hours})

    generated_at = now()
    current = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    cutoff = (current - timedelta(hours=since_hours)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tasks_rows = conn.execute(
        "SELECT t.* FROM tasks t WHERE t.state NOT IN ('INTEGRATED','ABANDONED') "
        "OR (t.state='INTEGRATED' AND EXISTS (SELECT 1 FROM events e WHERE e.entity_type='task' "
        "AND e.entity_id=t.task_id AND e.to_state='INTEGRATED' AND e.ts>=?)) "
        "ORDER BY t.updated_at DESC,t.task_id",
        (cutoff,),
    ).fetchall()

    tasks = []
    for task in tasks_rows:
        round_row = conn.execute(
            "SELECT round_id,n,actor,state,head_commit FROM rounds WHERE task_id=? ORDER BY n DESC LIMIT 1",
            (task["task_id"],),
        ).fetchone()
        latest_round = None
        round_state = None
        has_review = False
        if round_row is not None:
            round_state = round_row["state"]
            latest_round = {
                "n": round_row["n"],
                "actor": round_row["actor"],
                "state": round_state,
                "head": _short(round_row["head_commit"]),
            }
            has_review = conn.execute(
                "SELECT 1 FROM reviews WHERE round_id=? AND state!='STALE' LIMIT 1",
                (round_row["round_id"],),
            ).fetchone() is not None
        tasks.append({
            "task_id": task["task_id"],
            "repo_id": task["repo_id"],
            "state": task["state"],
            "goal": task["goal"],
            "latest_round": latest_round,
            "next_action": _next_action(task["state"], round_state, has_review),
            "updated_at": task["updated_at"],
        })

    pending_user_actions = []
    integrations = conn.execute(
        "SELECT i.integration_id,i.task_id FROM integrations i WHERE i.state='AWAITING_HUMAN_APPROVAL' "
        "ORDER BY i.created_at,i.integration_id"
    ).fetchall()
    for integration in integrations:
        pending_user_actions.append({
            "task_id": integration["task_id"],
            "kind": "integration_approval",
            "command": f"labops integrate approve --integration {integration['integration_id']}",
        })

    active_rows = conn.execute(
        "SELECT task_id FROM tasks WHERE state IN ('ACTIVE','READY_FOR_INTEGRATION') ORDER BY task_id"
    ).fetchall()
    for row in active_rows:
        result = evaluate(conn, row["task_id"])
        reasons = set(result["reasons"])
        if "self_review_needs_acceptance" in reasons:
            pending_user_actions.append({
                "task_id": row["task_id"],
                "kind": "self_review_acceptance",
                "command": f"labops approve self-review --task-id {row['task_id']}",
            })
        if "frozen_zone_touched" in reasons:
            pending_user_actions.append({
                "task_id": row["task_id"],
                "kind": "frozen_change_approval",
                "command": f"labops approve frozen --task-id {row['task_id']} --decision D-...",
            })

    recent_events = [dict(row) for row in conn.execute(
        "SELECT seq,entity_type,entity_id,to_state,ts FROM events WHERE ts>=? ORDER BY seq DESC LIMIT 30",
        (cutoff,),
    ).fetchall()]
    counts = {
        "active": conn.execute("SELECT COUNT(*) FROM tasks WHERE state='ACTIVE'").fetchone()[0],
        "ready": conn.execute("SELECT COUNT(*) FROM tasks WHERE state='READY_FOR_INTEGRATION'").fetchone()[0],
        "awaiting_approval": conn.execute(
            "SELECT COUNT(*) FROM integrations WHERE state='AWAITING_HUMAN_APPROVAL'"
        ).fetchone()[0],
        "integrated_in_window": conn.execute(
            "SELECT COUNT(DISTINCT entity_id) FROM events WHERE entity_type='task' "
            "AND to_state='INTEGRATED' AND ts>=?",
            (cutoff,),
        ).fetchone()[0],
    }
    runs_cutoff = current - timedelta(hours=24)
    run_candidates = conn.execute(
        "SELECT run_key,repo_id,run_dir,script_path,status,pid,started_at,finished_at,exit_code,"
        "progress_hint,progress_at,log_mtime,source,title,updated_at FROM bg_runs "
        "WHERE status='running' OR finished_at IS NOT NULL "
        "ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END,started_at DESC,run_key",
    ).fetchall()
    runs = []
    for run in run_candidates:
        if run["status"] == "running":
            runs.append(dict(run))
            continue
        try:
            finished = datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00"))
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            if finished.astimezone(timezone.utc) >= runs_cutoff.astimezone(timezone.utc):
                runs.append(dict(run))
        except (AttributeError, TypeError, ValueError):
            continue
    snapshot_version = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
    legacy = []
    for repo in conn.execute("SELECT repo_id FROM repos WHERE legacy_bridge IS NOT NULL ORDER BY repo_id"):
        status_rows = conn.execute(
            "SELECT key,value FROM legacy_records WHERE repo_id=? AND kind='status' "
            "AND key IN ('owner','state','task','round','updated') ORDER BY key",
            (repo["repo_id"],),
        ).fetchall()
        if not status_rows:
            continue
        item = {"repo_id": repo["repo_id"], "owner": None, "state": None, "task": None,
                "round": None, "updated": None}
        item.update({row["key"]: row["value"] for row in status_rows})
        legacy.append(item)
    result = {
        "generated_at": generated_at,
        "snapshot_version": snapshot_version,
        "tasks": tasks,
        "runs": runs,
        "queue": queue_progress(conn),
        "legacy": legacy,
        "pending_user_actions": pending_user_actions,
        "recent_events": recent_events,
        "counts": counts,
    }
    alerts_paused = _alerts_paused(conn)
    if alerts_paused is not None:
        result["alerts_paused"] = alerts_paused
    return result
