import json
import re
import sqlite3


_DROP_FIELDS = frozenset({
    "goal", "findings", "text", "path", "paths", "worktree_path", "branch", "argv",
    "log_path", "handoff", "brief", "submitted_ref", "repo_id", "cwd", "stderr", "ref",
})
_SHA_RE = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{40,64}(?![0-9a-f])")


def confidential_repo_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT repo_id FROM repos WHERE confidential=1")}


def _task_is_confidential(conn: sqlite3.Connection, task_id, confidential: set[str]) -> bool:
    if not isinstance(task_id, str):
        return False
    row = conn.execute("SELECT repo_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return bool(row and row["repo_id"] in confidential)


def _redact(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in _DROP_FIELDS:
                continue
            if key.endswith("_json") and isinstance(item, str):
                try:
                    item = json.loads(item)
                except json.JSONDecodeError:
                    continue
            result[key] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SHA_RE.sub(lambda match: match.group(0)[:12], value)
    return value


def _context_task_id(payload: dict) -> str | None:
    sections = payload.get("sections")
    task = sections.get("task") if isinstance(sections, dict) else None
    if isinstance(task, dict) and isinstance(task.get("task_id"), str):
        return task["task_id"]
    task_id = payload.get("task_id")
    return task_id if isinstance(task_id, str) else None


def for_actor(conn: sqlite3.Connection, actor: str, payload):
    if actor != "secretary" or not isinstance(payload, dict):
        return payload

    confidential = confidential_repo_ids(conn)
    task_id = _context_task_id(payload)
    if task_id and _task_is_confidential(conn, task_id, confidential):
        sections = payload.get("sections")
        handoff = sections.get("handoff") if isinstance(sections, dict) else None
        safe_handoff = None
        if isinstance(handoff, dict):
            if isinstance(handoff.get("rules"), str):
                safe_handoff = {"rules": handoff["rules"]}
            elif isinstance(handoff.get("ref"), str):
                safe_handoff = {"ref": handoff["ref"]}
        result = _redact(payload)
        if safe_handoff is not None and isinstance(result.get("sections"), dict):
            redacted_sections = {}
            for name, value in result["sections"].items():
                redacted_sections[name] = value
                if name == "task":
                    redacted_sections["handoff"] = safe_handoff
            result["sections"] = redacted_sections
        return result

    legacy = payload.get("legacy")
    if isinstance(legacy, list):
        result = dict(payload)
        redacted_legacy = []
        for item in legacy:
            if isinstance(item, dict) and item.get("repo_id") in confidential:
                if item.get("kind") == "status" or any(key in item for key in ("owner", "state", "task", "round", "updated")):
                    allowed = {"repo_id", "kind", "key", "ref", "owner", "state", "task", "round", "updated"}
                    redacted_legacy.append({key: value for key, value in item.items() if key in allowed})
                else:
                    allowed = {"repo_id", "kind", "key", "ref", "line"}
                    redacted_legacy.append({key: value for key, value in item.items() if key in allowed})
            else:
                redacted_legacy.append(item)
        result["legacy"] = redacted_legacy
        payload = result

    # progress_summary can include both confidential and ordinary repositories.
    tasks = payload.get("tasks")
    runs = payload.get("runs")
    queue = payload.get("queue")
    if isinstance(tasks, list) or isinstance(runs, list) or isinstance(queue, (list, dict)):
        result = dict(payload)
        if isinstance(tasks, list):
            redacted_tasks = []
            for task in tasks:
                if isinstance(task, dict) and task.get("repo_id") in confidential:
                    redacted_tasks.append(_redact(task))
                else:
                    redacted_tasks.append(task)
            result["tasks"] = redacted_tasks
        if isinstance(runs, list):
            result["runs"] = [
                {key: item[key] for key in ("status", "started_at", "finished_at") if key in item}
                if isinstance(item, dict) and item.get("repo_id") in confidential else item
                for item in runs
            ]
        if isinstance(queue, list):
            result["queue"] = [
                {key: item[key] for key in ("title", "status") if key in item}
                if isinstance(item, dict) and item.get("repo_id") in confidential else item
                for item in queue
            ]
        elif isinstance(queue, dict) and isinstance(queue.get("items"), list):
            items = [
                {key: item[key] for key in ("title", "status") if key in item}
                if isinstance(item, dict) and item.get("repo_id") in confidential else item
                for item in queue["items"]
            ]
            result["queue"] = {**queue, "count": len(items), "items": items}
        gpu = payload.get("gpu")
        if isinstance(gpu, dict) and isinstance(gpu.get("runs"), list):
            result["gpu"] = {**gpu, "runs": [item for item in gpu["runs"]
                                               if not isinstance(item, dict)
                                               or item.get("repo_id") not in confidential]}
        return result

    # SHA history has no task selector. It contains no narrative fields, but still
    # shortens any full hashes before a secretary sees them.
    if "matches" in payload:
        return _redact(payload)
    return payload
