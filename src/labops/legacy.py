import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import gitio
from .canon import canonical_json
from .config import now
from .db import tx
from .errors import ValidationError
from .tasks import repo_row


_STATUS_KEYS = {"owner", "state", "task", "round", "updated", "baseline", "spec"}
_STATUS_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*?)\s*$")
_DECISION_RE = re.compile(r"^##\s+(D-\d{8}-\d+)(?:\s+(.*?))?\s*$")
_TASK_RE = re.compile(r"T\d{8}-[a-z0-9-]+")


def _safe_within(path: Path, root: Path) -> Path | None:
    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _hash_line(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8", "surrogatepass")).hexdigest()


def _row(repo_id: str, kind: str, key: str, path: str, line_number: int,
         *, ref: str | None = None, title: str | None = None, value: str | None = None,
         source_line: str) -> dict:
    return {"repo_id": repo_id, "kind": kind, "key": key, "ref": ref, "path": path, "line": line_number,
            "title": title, "value": value, "content_hash": _hash_line(source_line), "imported_at": now()}


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def import_legacy(conn: sqlite3.Connection, repo_id: str) -> dict:
    repo = repo_row(conn, repo_id)
    if not repo["legacy_bridge"]:
        raise ValidationError("legacy_bridge_not_configured", detail={"repo_id": repo_id})
    repo_root = Path(repo["path"]).resolve()
    bridge_candidate = repo_root / repo["legacy_bridge"]
    bridge = _safe_within(bridge_candidate, repo_root)
    if bridge is None or not bridge.is_dir():
        raise ValidationError("legacy_bridge_path_invalid", detail={"repo_id": repo_id})
    rows = []

    status_path = _safe_within(bridge / "agent-status.md", repo_root)
    if status_path is not None and status_path.is_file():
        relative = status_path.relative_to(repo_root).as_posix()
        with status_path.open("r", encoding="utf-8", errors="replace") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                source_line = raw_line.rstrip("\r\n")
                if not source_line.strip() or source_line.startswith("#"):
                    break
                match = _STATUS_LINE_RE.match(source_line)
                if not match:
                    break
                key, value = match.groups()
                if key in _STATUS_KEYS:
                    rows.append(_row(repo_id, "status", key, relative, line_number,
                                     value=value[:200], source_line=source_line))

    decisions_path = _safe_within(bridge / "decisions.md", repo_root)
    if decisions_path is not None and decisions_path.is_file():
        relative = decisions_path.relative_to(repo_root).as_posix()
        with decisions_path.open("r", encoding="utf-8", errors="replace") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                source_line = raw_line.rstrip("\r\n")
                match = _DECISION_RE.match(source_line)
                if match:
                    key, title = match.groups()
                    rows.append(_row(repo_id, "decision", f"{key}@L{line_number}", relative, line_number, ref=key,
                                     title=(title or "").strip(), source_line=source_line))

    review_dir = _safe_within(bridge / "review", repo_root)
    if review_dir is not None and review_dir.is_dir():
        for candidate in sorted(review_dir.glob("*.md"), key=lambda item: item.name):
            path = _safe_within(candidate, repo_root)
            match = _TASK_RE.search(candidate.name)
            if path is None or not path.is_file() or not match:
                continue
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                first_line = stream.readline().rstrip("\r\n")
            if not first_line.startswith("# "):
                continue
            relative = path.relative_to(repo_root).as_posix()
            rows.append(_row(repo_id, "review", match.group(0), relative, 1,
                             title=first_line[2:].strip(), source_line=first_line))

    imported_at = now()
    with tx(conn):
        conn.executemany(
            "INSERT INTO legacy_records(repo_id,kind,key,ref,path,line,title,value,content_hash,imported_at) "
            "VALUES(:repo_id,:kind,:key,:ref,:path,:line,:title,:value,:content_hash,:imported_at) "
            "ON CONFLICT(repo_id,kind,key) DO UPDATE SET ref=excluded.ref,path=excluded.path,line=excluded.line,"
            "title=excluded.title,value=excluded.value,content_hash=excluded.content_hash,imported_at=excluded.imported_at "
            "WHERE legacy_records.ref IS NOT excluded.ref OR legacy_records.path!=excluded.path "
            "OR legacy_records.line IS NOT excluded.line OR legacy_records.title IS NOT excluded.title "
            "OR legacy_records.value IS NOT excluded.value OR legacy_records.content_hash!=excluded.content_hash",
            rows,
        )
    by_kind = {}
    for item in rows:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    return {"repo_id": repo_id, "imported": len(rows), "by_kind": by_kind,
            "records": rows, "imported_at": imported_at}


def _parse_updated(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _record_drift(conn: sqlite3.Connection, repo_id: str, detail: dict) -> bool:
    expected = canonical_json(detail).decode("utf-8")
    rows = conn.execute(
        "SELECT detail_json FROM events WHERE entity_type='repo' AND entity_id=? AND to_state='legacy_drift'",
        (repo_id,),
    ).fetchall()
    for row in rows:
        try:
            if canonical_json(json.loads(row["detail_json"] or "{}")).decode("utf-8") == expected:
                return False
        except (TypeError, json.JSONDecodeError):
            continue
    with tx(conn):
        conn.execute(
            "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
            (now(), "repo", repo_id, None, "legacy_drift", "labops", expected),
        )
    return True


def audit_legacy(conn: sqlite3.Connection, repo_id: str) -> dict:
    imported = import_legacy(conn, repo_id)
    repo = repo_row(conn, repo_id)
    repo_path = Path(repo["path"])
    status = {
        row["key"]: row["value"] for row in conn.execute(
            "SELECT key,value FROM legacy_records WHERE repo_id=? AND kind='status'", (repo_id,)
        ).fetchall()
    }
    detected = []
    baseline = status.get("baseline")
    head = gitio.rev_parse(repo_path, "HEAD")
    if not baseline:
        detail = {"kind": "baseline_missing"}
        if _record_drift(conn, repo_id, detail):
            detected.append(detail)
    elif baseline != head:
        resolved_baseline = gitio.rev_parse(repo_path, f"{baseline}^{{commit}}")
        if resolved_baseline is None:
            kind = "baseline_invalid"
        elif gitio.is_ancestor(repo_path, resolved_baseline, head):
            kind = "behind"
        else:
            kind = "baseline_diverged"
        detail = {"kind": kind, "baseline": baseline[:12], "head": head[:12] if head else None}
        if _record_drift(conn, repo_id, detail):
            detected.append(detail)

    dirty = gitio.status_porcelain(repo_path, untracked="no")
    if dirty:
        detail = {"kind": "dirty_worktree"}
        if _record_drift(conn, repo_id, detail):
            detected.append(detail)

    updated = _parse_updated(status.get("updated"))
    if updated is not None:
        current = datetime.fromisoformat(now().replace("Z", "+00:00")).astimezone(timezone.utc)
        if current - updated > timedelta(days=3):
            detail = {"kind": "stale_updated", "updated": status["updated"][:200]}
            if _record_drift(conn, repo_id, detail):
                detected.append(detail)
    return {"repo_id": repo_id, "imported": imported["imported"], "by_kind": imported["by_kind"],
            "records": imported["records"], "detected": detected}
