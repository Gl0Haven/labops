import json
import sqlite3

from .errors import ValidationError
from .tasks import task_row

_SHA_COLUMNS = (
    ("rounds", "round_id", "head_commit"),
    ("rounds", "round_id", "base_commit"),
    ("claims", "claim_id", "fingerprint"),
    ("evidence", "evidence_id", "commit_oid"),
    ("reviews", "review_id", "reviewed_commit"),
    ("integrations", "integration_id", "candidate_commit"),
    ("integrations", "integration_id", "main_before"),
    ("approvals", "approval_id", "target_commit"),
)


def _dicts(rows, *, omit: set[str] | None = None) -> list[dict]:
    omitted = omit or set()
    return [{key: value for key, value in dict(row).items() if key not in omitted} for row in rows]


def _task_history(conn: sqlite3.Connection, task_id: str, limit: int) -> dict:
    task = task_row(conn, task_id)
    round_rows = conn.execute("SELECT * FROM rounds WHERE task_id=? ORDER BY created_at,round_id LIMIT ?", (task_id, limit)).fetchall()
    all_round_ids = [row[0] for row in conn.execute("SELECT round_id FROM rounds WHERE task_id=? ORDER BY n", (task_id,))]
    claims = conn.execute(
        "SELECT c.* FROM claims c JOIN rounds r ON r.round_id=c.round_id WHERE r.task_id=? "
        "ORDER BY c.created_at,c.claim_id LIMIT ?",
        (task_id, limit),
    ).fetchall()
    reviews = conn.execute(
        "SELECT * FROM reviews WHERE task_id=? ORDER BY created_at,review_id LIMIT ?", (task_id, limit)
    ).fetchall()
    review_items = []
    for row in reviews:
        item = dict(row)
        findings = json.loads(item.pop("findings_json") or "[]")
        item["findings_count"] = len(findings) if isinstance(findings, list) else 0
        review_items.append(item)
    evidence = conn.execute(
        "SELECT e.* FROM evidence e JOIN rounds r ON r.round_id=e.round_id WHERE r.task_id=? "
        "ORDER BY e.created_at,e.evidence_id LIMIT ?",
        (task_id, limit),
    ).fetchall()
    integrations = conn.execute(
        "SELECT * FROM integrations WHERE task_id=? ORDER BY created_at,integration_id LIMIT ?", (task_id, limit)
    ).fetchall()
    approvals = conn.execute(
        "SELECT * FROM approvals WHERE task_id=? ORDER BY created_at,approval_id LIMIT ?", (task_id, limit)
    ).fetchall()
    review_ids = [row[0] for row in conn.execute("SELECT review_id FROM reviews WHERE task_id=?", (task_id,))]
    integration_ids = [row[0] for row in conn.execute("SELECT integration_id FROM integrations WHERE task_id=?", (task_id,))]
    ids = [task_id, *all_round_ids, *review_ids, *integration_ids]
    event_placeholders = ",".join("?" for _ in ids)
    events = conn.execute(
        f"SELECT * FROM events WHERE entity_id IN ({event_placeholders}) ORDER BY seq LIMIT ?", (*ids, limit)
    ).fetchall()
    return {
        "task_id": task["task_id"],
        "rounds": _dicts(round_rows),
        "claims": _dicts(claims, omit={"payload_json"}),
        "reviews": review_items,
        "evidence": _dicts(evidence, omit={"env_json"}),
        "integrations": _dicts(integrations),
        "approvals": _dicts(approvals),
        "events": _dicts(events),
    }


def _sha_history(conn: sqlite3.Connection, sha: str) -> dict:
    if not isinstance(sha, str) or not 7 <= len(sha) <= 64 or any(ch not in "0123456789abcdefABCDEF" for ch in sha):
        raise ValidationError("invalid_sha_prefix", detail={"sha": sha})
    prefix = sha.lower()
    matches = []
    full_values = set()
    for table, id_column, column in _SHA_COLUMNS:
        predicate = f"lower({column}) LIKE ?"
        pattern = prefix + "%"
        if table == "claims" and column == "fingerprint":
            predicate = f"lower({column}) LIKE ?"
            pattern = "%|" + prefix + "%"
        rows = conn.execute(
            f"SELECT {id_column},{column} FROM {table} WHERE {predicate} ORDER BY {id_column}",
            (pattern,),
        ).fetchall()
        for row in rows:
            value = row[column]
            if value is None:
                continue
            full_value = value.rsplit("|", 1)[-1] if table == "claims" and column == "fingerprint" else value
            full_values.add(full_value.lower())
            matches.append({"table": table, "id": row[id_column], "column": column, "value": value})
    return {"sha": prefix, "matches": matches, "ambiguous": len(full_values) > 1}


def query_history(conn: sqlite3.Connection, *, task_id: str | None = None, sha: str | None = None,
                  limit: int = 50) -> dict:
    if (task_id is None) == (sha is None):
        raise ValidationError("history_requires_one_selector")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValidationError("invalid_history_limit", detail={"limit": limit})
    if task_id is not None:
        exists = conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if exists:
            return _task_history(conn, task_id, limit)
        repo_ids = [row[0] for row in conn.execute(
            "SELECT DISTINCT repo_id FROM legacy_records WHERE kind='review' AND key=? ORDER BY repo_id",
            (task_id,),
        ).fetchall()]
        if not repo_ids:
            return {"task_id": task_id, "legacy": []}
        placeholders = ",".join("?" for _ in repo_ids)
        rows = conn.execute(
            "SELECT repo_id,kind,key,ref,path,line,title,value,content_hash,imported_at FROM legacy_records "
            f"WHERE (kind='review' AND key=?) OR (kind='decision' AND repo_id IN ({placeholders})) "
            "ORDER BY repo_id,kind,key LIMIT ?",
            (task_id, *repo_ids, limit),
        ).fetchall()
        return {"task_id": task_id, "legacy": [dict(row) for row in rows]}
    return _sha_history(conn, sha)
