import json
import math
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from .errors import ValidationError


_SEVERITIES = ("blocker", "major", "minor", "note")


def _cutoff(since: str | None) -> str | None:
    if since is None:
        return None
    if not isinstance(since, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        raise ValidationError("invalid_stats_since", detail={"since": since})
    try:
        date.fromisoformat(since)
    except ValueError as exc:
        raise ValidationError("invalid_stats_since", detail={"since": since}) from exc
    return f"{since}T00:00:00Z"


def _timestamp(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _submission_stats(claim_rows) -> dict:
    # Only claims written at schema v9 or later carry a source; older rows are reported as
    # unknown instead of being guessed into either bucket.
    by_source: dict[str, int] = {}
    for row in claim_rows:
        source = row["source"] if "source" in row.keys() else None
        key = source or "unknown"
        by_source[key] = by_source.get(key, 0) + 1
    known = by_source.get("mcp", 0) + by_source.get("cli", 0)
    return {"claim_count": len(claim_rows), "by_source": dict(sorted(by_source.items())),
            "mcp_claim_share": by_source.get("mcp", 0) / known if known else None,
            "attributed_count": known}


def _median(values: list[int | float]) -> int | float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _p90(values: list[int | float]) -> int | float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def _duration_ms(results_json: str | None) -> int | None:
    try:
        results = json.loads(results_json or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(results, list):
        return None
    durations = [item["duration_ms"] for item in results
                 if isinstance(item, dict) and isinstance(item.get("duration_ms"), int)
                 and not isinstance(item.get("duration_ms"), bool) and item["duration_ms"] >= 0]
    return sum(durations) if durations else None


def _rows_since(conn: sqlite3.Connection, table: str, column: str, cutoff: str | None,
                where: str = "", parameters: tuple = ()) -> list[sqlite3.Row]:
    clauses = [where] if where else []
    args = list(parameters)
    if cutoff is not None:
        clauses.append(f"{column}>=?")
        args.append(cutoff)
    predicate = " WHERE " + " AND ".join(clauses) if clauses else ""
    return conn.execute(f"SELECT * FROM {table}{predicate}", args).fetchall()


def stats_report(conn: sqlite3.Connection, *, since: str | None = None) -> dict:
    cutoff = _cutoff(since)
    tasks = _rows_since(conn, "tasks", "created_at", cutoff)
    task_states: dict[str, int] = {}
    for task in tasks:
        task_states[task["state"]] = task_states.get(task["state"], 0) + 1

    rounds_by_task = []
    rounds_total = 0
    rework_rounds_total = 0
    for task in sorted(tasks, key=lambda row: row["task_id"]):
        rounds = conn.execute("SELECT round_id,n,state FROM rounds WHERE task_id=? ORDER BY n", (task["task_id"],)).fetchall()
        rework_rounds = 0
        for index, round_row in enumerate(rounds):
            if index == 0:
                continue
            previous = rounds[index - 1]
            review_required = conn.execute(
                "SELECT 1 FROM reviews WHERE round_id=? AND verdict='REWORK_REQUIRED' LIMIT 1",
                (previous["round_id"],),
            ).fetchone() is not None
            if previous["state"] == "REJECTED" or review_required:
                rework_rounds += 1
        rounds_total += len(rounds)
        rework_rounds_total += rework_rounds
        rounds_by_task.append({"task_id": task["task_id"], "rounds": len(rounds),
                               "rework_rounds": rework_rounds})

    claim_rows = _rows_since(conn, "claims", "created_at", cutoff, "kind='submit_changes'")
    reviews = _rows_since(conn, "reviews", "updated_at", cutoff)
    verdicts: dict[str, int] = {}
    findings = {severity: 0 for severity in _SEVERITIES}
    self_reviews = 0
    for review in reviews:
        verdict = review["verdict"]
        if verdict:
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
        if review["independence"] == "SELF_REVIEWED_FIX":
            self_reviews += 1
        try:
            items = json.loads(review["findings_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            items = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("severity") in findings:
                    findings[item["severity"]] += 1

    evidence = conn.execute(
        "SELECT status,results_json FROM evidence WHERE kind='INDEPENDENTLY_EXECUTED'" +
        (" AND created_at>=?" if cutoff is not None else ""),
        (cutoff,) if cutoff is not None else (),
    ).fetchall()
    completed = [row for row in evidence if row["status"] != "running"]
    passed = sum(row["status"] == "passed" for row in completed)
    durations = [duration for row in completed
                 if (duration := _duration_ms(row["results_json"])) is not None]

    merged_where = "state='MERGED'"
    integrations = _rows_since(conn, "integrations", "updated_at", cutoff, merged_where)
    integration_durations = []
    event_where = "entity_type='task' AND to_state='INTEGRATED'"
    event_args: list[str] = []
    if cutoff is not None:
        event_where += " AND ts>=?"
        event_args.append(cutoff)
    integrated_events = conn.execute(
        "SELECT entity_id,MIN(ts) AS integrated_at FROM events WHERE " + event_where + " GROUP BY entity_id",
        event_args,
    ).fetchall()
    for event in integrated_events:
        task = conn.execute("SELECT created_at FROM tasks WHERE task_id=?", (event["entity_id"],)).fetchone()
        created_at = _timestamp(task["created_at"] if task else None)
        integrated_at = _timestamp(event["integrated_at"])
        if created_at is not None and integrated_at is not None:
            seconds = (integrated_at - created_at).total_seconds()
            if seconds >= 0:
                integration_durations.append(seconds)

    notifications = _rows_since(conn, "notifications", "created_at", cutoff)
    notification_statuses: dict[str, int] = {}
    rewrite_outcomes: dict[str, int] = {}
    for notification in notifications:
        status = notification["status"]
        notification_statuses[status] = notification_statuses.get(status, 0) + 1
        outcome = notification["rewrite_outcome"] if "rewrite_outcome" in notification.keys() else None
        if outcome:
            rewrite_outcomes[outcome] = rewrite_outcomes.get(outcome, 0) + 1
    notification_columns = {row["name"] for row in conn.execute("PRAGMA table_info(notifications)")}
    ntfy_statuses = None
    if "ntfy_status" in notification_columns:
        ntfy_statuses = {}
        for notification in _rows_since(conn, "notifications", "created_at", cutoff):
            status = notification["ntfy_status"]
            if status:
                ntfy_statuses[status] = ntfy_statuses.get(status, 0) + 1

    repo_count = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
    backup_count = conn.execute("SELECT COUNT(*) FROM backups").fetchone()[0]
    run_statuses = {row["status"]: row["count"] for row in conn.execute(
        "SELECT status,COUNT(*) AS count FROM bg_runs GROUP BY status ORDER BY status"
    )}
    run_count = conn.execute("SELECT COUNT(*) FROM bg_runs").fetchone()[0]
    last_restore_row = conn.execute("SELECT value FROM meta WHERE key='last_verify_restore'").fetchone()
    last_restore = None
    if last_restore_row is not None:
        try:
            parsed_restore = json.loads(last_restore_row["value"])
        except (TypeError, json.JSONDecodeError):
            parsed_restore = None
        if isinstance(parsed_restore, dict):
            last_restore = parsed_restore
    return {
        "since": since,
        "tasks": {"total": len(tasks), "by_state": dict(sorted(task_states.items())),
                  "rounds_total": rounds_total, "rework_rounds_total": rework_rounds_total,
                  "rounds_by_task": rounds_by_task},
        "submissions": _submission_stats(claim_rows),
        "reviews": {"by_verdict": dict(sorted(verdicts.items())),
                    "findings_by_severity": findings, "self_review_count": self_reviews},
        "verification": {"count": len(evidence), "completed": len(completed), "passed": passed,
                         "pass_rate": passed / len(completed) if completed else None,
                         "duration_samples": len(durations), "median_duration_ms": _median(durations),
                         "p90_duration_ms": _p90(durations)},
        "integrations": {"merged_count": len(integrations),
                         "median_time_to_integrated_seconds": _median(integration_durations),
                         "duration_samples": len(integration_durations)},
        "alerts": {"by_status": dict(sorted(notification_statuses.items())),
                   "ntfy_by_status": ntfy_statuses,
                   "rewrite_by_outcome": dict(sorted(rewrite_outcomes.items()))},
        "runs": {"tracked_count": run_count, "by_status": run_statuses},
        "repositories": {"registered_count": repo_count, "backup_count": backup_count,
                         "last_verify_restore": last_restore},
    }


def render_markdown(report: dict) -> str:
    tasks = report["tasks"]
    verification = report["verification"]
    integrations = report["integrations"]
    repositories = report["repositories"]
    lines = ["# LabOps stats", "", f"Since: {report['since'] or 'all records'}", "",
             "## Tasks", "", "| Metric | Value |", "| --- | ---: |",
             f"| Total | {tasks['total']} |",
             f"| Rounds | {tasks['rounds_total']} |",
             f"| Rework rounds | {tasks['rework_rounds_total']} |"]
    for state, count in tasks["by_state"].items():
        lines.append(f"| State: {state} | {count} |")
    lines.extend(["", "| Task | Rounds | Rework rounds |", "| --- | ---: | ---: |"])
    for item in tasks["rounds_by_task"]:
        lines.append(f"| {item['task_id']} | {item['rounds']} | {item['rework_rounds']} |")
    lines.extend(["", "## Submissions", "",
                  f"Submit changes claims: {report['submissions']['claim_count']}", "",
                  "MCP claim share: unavailable (origin is not persisted).", "",
                  "## Reviews", "", "| Verdict | Count |", "| --- | ---: |"])
    for verdict, count in report["reviews"]["by_verdict"].items():
        lines.append(f"| {verdict} | {count} |")
    lines.extend(["", "Findings by severity:", ""])
    for severity, count in report["reviews"]["findings_by_severity"].items():
        lines.append(f"- {severity}: {count}")
    lines.extend(["", f"Self reviews: {report['reviews']['self_review_count']}", "",
                  "## Verification", "", f"Attempts: {verification['count']}",
                  f"Completed: {verification['completed']}", f"Passed: {verification['passed']}",
                  f"Pass rate: {verification['pass_rate']}",
                  f"Median duration (ms): {verification['median_duration_ms']}",
                  f"P90 duration (ms): {verification['p90_duration_ms']}", "",
                  "## Integrations", "", f"Merged: {integrations['merged_count']}",
                  f"Median task-to-integrated time (seconds): {integrations['median_time_to_integrated_seconds']}", "",
                  "## Alerts", "", "| Status | Count |", "| --- | ---: |"])
    for status, count in report["alerts"]["by_status"].items():
        lines.append(f"| {status} | {count} |")
    lines.extend(["", "Rewrite outcomes:"])
    for outcome, count in report["alerts"].get("rewrite_by_outcome", {}).items():
        lines.append(f"- {outcome}: {count}")
    runs = report.get("runs", {"tracked_count": 0, "by_status": {}})
    lines.extend(["", "## Background runs", "", f"Tracked: {runs['tracked_count']}"])
    for status, count in runs["by_status"].items():
        lines.append(f"- {status}: {count}")
    lines.extend(["", "## Repositories", "", f"Registered: {repositories['registered_count']}",
                  f"Backups: {repositories['backup_count']}",
                  f"Last verify-restore: {repositories['last_verify_restore'] or 'unknown'}", ""])
    return "\n".join(lines)


def write_markdown(report: dict, path: str | Path) -> str:
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_markdown(report), encoding="utf-8")
    return str(output)
