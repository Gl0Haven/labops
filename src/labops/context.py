import json
import math
import sqlite3
from pathlib import Path

from .canon import canonical_json
from .config import home
from .errors import ValidationError
from .tasks import get_handoff_rules, round_row, task_row


def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff")
    return cjk + math.ceil((len(text) - cjk) / 4)


def _encoded_tokens(value: dict | list | None) -> int:
    return estimate_tokens(canonical_json(value).decode("utf-8"))


def _next_action(task_state: str, round_state: str, has_review: bool) -> str | None:
    if task_state == "INTEGRATED":
        return "done"
    if task_state == "ABANDONED":
        return "abandoned"
    if round_state == "VERIFIED" and has_review and task_state == "READY_FOR_INTEGRATION":
        return "await_integration_approval"
    if round_state == "OPEN":
        return "implement_then_snapshot_and_submit"
    if round_state in {"CLAIMED", "VERIFYING"}:
        return "await_verify"
    if round_state == "VERIFIED":
        return "await_review" if not has_review else "await_gate_check"
    if round_state == "REJECTED":
        return "rework_in_new_round"
    if round_state == "BLOCKED":
        return "resolve_block"
    return None


def _task_section(conn: sqlite3.Connection, task) -> dict:
    profile = conn.execute("SELECT content_json FROM acceptance_profiles WHERE profile_hash=?", (task["profile_hash"],)).fetchone()
    if profile is None:
        raise ValidationError("profile_not_found", detail={"profile_hash": task["profile_hash"]})
    content = json.loads(profile[0])
    return {
        "task_id": task["task_id"],
        "goal": task["goal"],
        "state": task["state"],
        "base_commit": task["base_commit"],
        "profile_hash": task["profile_hash"],
        "acceptance": [{"name": command["name"], "argv": command["argv"]}
                       for command in content.get("commands", [])],
    }


def _current_section(conn: sqlite3.Connection, task) -> tuple[dict | None, dict | None]:
    task_id = task["task_id"]
    rnd = conn.execute("SELECT * FROM rounds WHERE task_id=? ORDER BY n DESC LIMIT 1", (task_id,)).fetchone()
    if rnd is None:
        return None, None
    review = conn.execute(
        "SELECT review_id FROM reviews WHERE round_id=? AND state!='STALE' ORDER BY created_at DESC,review_id DESC LIMIT 1",
        (rnd["round_id"],),
    ).fetchone()
    submitted_ref = f"refs/labops/{task_id}/r{rnd['n']}/{rnd['actor']}/submitted"
    section = {
        "round_id": rnd["round_id"],
        "n": rnd["n"],
        "actor": rnd["actor"],
        "role": rnd["role"],
        "state": rnd["state"],
        "base_commit": rnd["base_commit"],
        "head_commit": rnd["head_commit"],
        "head_tree": rnd["head_tree"],
        "branch": rnd["branch"],
        "worktree_path": rnd["worktree_path"],
        "submitted_ref": submitted_ref,
        "next_action": _next_action(task["state"], rnd["state"], review is not None),
    }
    return section, rnd


def _open_findings(conn: sqlite3.Connection, task_id: str) -> tuple[dict, list[str]]:
    review = conn.execute(
        "SELECT review_id,findings_json FROM reviews WHERE task_id=? AND state!='STALE' "
        "ORDER BY created_at DESC,review_id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if review is None:
        return {"review_id": None, "findings": []}, []
    findings = json.loads(review["findings_json"] or "[]")
    rank = {"blocker": 0, "major": 1}
    selected = [item for item in findings if isinstance(item, dict) and item.get("severity") in rank]
    selected.sort(key=lambda item: (rank[item["severity"]], item.get("id", "")))
    return {"review_id": review["review_id"], "findings": selected}, [review["review_id"]]


def _last_verification(conn: sqlite3.Connection, task_id: str) -> dict | None:
    row = conn.execute(
        "SELECT e.evidence_id,e.status,e.results_json FROM evidence e "
        "JOIN rounds r ON r.round_id=e.round_id WHERE r.task_id=? "
        "ORDER BY e.created_at DESC,e.evidence_id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    results = json.loads(row["results_json"] or "[]")
    return {
        "evidence_id": row["evidence_id"],
        "status": row["status"],
        "results": [{"name": item.get("name"), "exit_code": item.get("exit_code"),
                     "timed_out": item.get("timed_out")}
                    for item in results if isinstance(item, dict) and "name" in item],
    }


def _policy_section(task) -> dict:
    if not task["policy_json"]:
        return {"frozen": []}
    policy = json.loads(task["policy_json"])
    frozen = policy.get("frozen", []) if isinstance(policy, dict) else []
    return {"frozen": [{key: item[key] for key in ("path", "symbols", "ref") if key in item}
                       for item in frozen if isinstance(item, dict)]}


def _recent_events(conn: sqlite3.Connection, task_id: str) -> tuple[list[dict], int]:
    round_ids = [row[0] for row in conn.execute("SELECT round_id FROM rounds WHERE task_id=?", (task_id,))]
    related_ids = [task_id, *round_ids]
    placeholders = ",".join("?" for _ in related_ids)
    rows = conn.execute(
        f"SELECT seq,entity_type,entity_id,from_state,to_state FROM events "
        f"WHERE entity_id IN ({placeholders}) ORDER BY seq DESC LIMIT 10",
        related_ids,
    ).fetchall()
    max_row = conn.execute(f"SELECT MAX(seq) FROM events WHERE entity_id IN ({placeholders})", related_ids).fetchone()
    return [dict(row) for row in rows], int(max_row[0] or 0)


def _alerts_paused(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM meta WHERE key='wechat_paused_at'").fetchone()
    if row is None or not row["value"]:
        return None
    return {"since": row["value"], "hint": "微信凭证失效：请给 bot 发一句话"}


def task_context(conn: sqlite3.Connection, *, task_id: str | None = None, round_id: str | None = None,
                 budget_tokens: int = 4000) -> dict:
    if (task_id is None) == (round_id is None):
        raise ValidationError("task_context_requires_one_selector")
    if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or not 500 <= budget_tokens <= 32000:
        raise ValidationError("invalid_context_budget", detail={"budget_tokens": budget_tokens})
    if round_id is not None:
        rnd = round_row(conn, round_id)
        task_id = rnd["task_id"]
    task = task_row(conn, task_id)
    current, rnd = _current_section(conn, task)
    open_findings, review_ids = _open_findings(conn, task_id)
    recent_events, snapshot_version = _recent_events(conn, task_id)
    sections = {
        "task": _task_section(conn, task),
        "handoff": {"rules": get_handoff_rules(conn), "brief": task["brief"]},
        "current": current,
        "open_findings": open_findings,
        "last_verification": _last_verification(conn, task_id),
        "policy": _policy_section(task),
        "recent_events": recent_events,
    }
    references = {
        "task": task_id,
        "handoff": task_id,
        "current": current["round_id"] if current else task_id,
        "open_findings": open_findings["review_id"] or task_id,
        "last_verification": sections["last_verification"]["evidence_id"] if sections["last_verification"] else task_id,
        "policy": task["policy_hash"] or task_id,
        "recent_events": f"{task_id}:{snapshot_version}",
    }
    names = list(sections)
    reference_costs = {name: _encoded_tokens({"ref": references[name]}) for name in names}
    included: dict[str, dict | list | None] = {}
    truncated: list[str] = []
    used_tokens = 0
    for index, (name, value) in enumerate(sections.items()):
        cost = _encoded_tokens(value)
        reserved_for_later_refs = sum(reference_costs[next_name] for next_name in names[index + 1:])
        if used_tokens + cost + reserved_for_later_refs <= budget_tokens:
            included[name] = value
            used_tokens += cost
            continue
        truncated.append(name)
        reference = {"ref": references[name]}
        included[name] = reference
        used_tokens += reference_costs[name]
    handoff_path = None
    if rnd is not None:
        candidate = home() / "handoffs" / task_id / f"r{rnd['n']}.json"
        if candidate.is_file():
            handoff_path = str(candidate)
    result = {
        "snapshot_version": snapshot_version,
        "budget_tokens": budget_tokens,
        "used_tokens": used_tokens,
        "sections": included,
        "truncated": truncated,
        "refs": {"handoff": handoff_path, "review_ids": review_ids},
    }
    alerts_paused = _alerts_paused(conn)
    if alerts_paused is not None:
        result["alerts_paused"] = alerts_paused
    return result
