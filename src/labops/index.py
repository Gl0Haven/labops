import hashlib
import json
import logging
import sqlite3
from pathlib import Path

from . import gitio
from .config import now
from .db import tx
from .errors import ValidationError
from .search import bigram_text
from .tasks import repo_row


logger = logging.getLogger("labops.index")
_MAX_MARKDOWN_FILE_BYTES = 2 * 1024 * 1024


def _doc(repo_id: str, kind: str, ref: str, *, path: str | None, line: int | None,
         title: str | None, body: str, ts: str | None, confidential: bool,
         superseded_by: str | None = None) -> dict:
    identity = "|".join((repo_id, kind, ref, path or "", str(line or "")))
    doc_id = hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()[:24]
    indexed_text = "\n".join(value for value in (title, body) if value)
    content = "|".join((title or "", body, bigram_text(indexed_text), ts or "",
                        str(int(confidential)), superseded_by or ""))
    return {"doc_id": doc_id, "repo_id": repo_id, "kind": kind, "ref": ref,
            "path": path, "line": line, "title": title, "body": body, "ts": ts,
            "content_hash": hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest(),
            "superseded_by": superseded_by,
            "confidential": int(confidential), "indexed_at": now(), "text": indexed_text}


def _safe_file(repo_root: Path, relative: str) -> Path | None:
    candidate = (repo_root / relative).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _legacy_decision_body(path: Path, line: int, fallback: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return fallback[:4000]
    start = line - 1
    if start < 0 or start >= len(lines):
        return fallback[:4000]
    content = []
    for current in lines[start + 1:]:
        if current.startswith("## "):
            break
        content.append(current)
        if sum(len(item) + 1 for item in content) >= 4000:
            break
    return "\n".join(content)[:4000] or fallback[:4000]


def _markdown_plan_docs(repo_id: str, repo_root: Path, confidential: bool) -> list[dict]:
    bridge = (repo_root / ".ai-bridge").resolve()
    try:
        bridge.relative_to(repo_root.resolve())
    except ValueError:
        return []
    candidates = [bridge / "current-plan.md"]
    review_dir = (bridge / "review").resolve()
    try:
        review_dir.relative_to(repo_root.resolve())
    except ValueError:
        review_dir = None
    if review_dir is not None and review_dir.is_dir():
        candidates.extend(sorted(review_dir.glob("*plan-archive*.md"), key=lambda item: item.name))
    docs = []
    for candidate in candidates:
        safe = _safe_file(repo_root, candidate.relative_to(repo_root).as_posix())
        if safe is None:
            continue
        try:
            lines = safe.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        relative = safe.relative_to(repo_root).as_posix()
        headings = [(number, line) for number, line in enumerate(lines, start=1)
                    if line.startswith("# ")]
        for number, heading in headings:
            title = heading.lstrip("# ").strip()
            docs.append(_doc(repo_id, "plan", f"{relative}#L{number}", path=relative, line=number,
                             title=title, body=heading, ts=None, confidential=confidential))
        section_starts = [(number, line) for number, line in enumerate(lines, start=1)
                          if line.startswith("## ")]
        for index, (number, heading) in enumerate(section_starts):
            end = section_starts[index + 1][0] - 1 if index + 1 < len(section_starts) else len(lines)
            section = "\n".join(lines[number - 1:end])[:1000]
            docs.append(_doc(repo_id, "plan", f"{relative}#L{number}", path=relative, line=number,
                             title=heading[3:].strip(), body=section, ts=None, confidential=confidential))
    return docs


def _markdown_section_docs(repo_id: str, repo_root: Path, directory: str, kind: str,
                           confidential: bool) -> tuple[list[dict], list[dict]]:
    candidate_dir = (repo_root / directory).resolve()
    try:
        candidate_dir.relative_to(repo_root.resolve())
    except ValueError:
        return [], []
    if not candidate_dir.is_dir():
        return [], []
    docs, skipped = [], []
    for entry in sorted(candidate_dir.glob("*.md"), key=lambda item: item.name):
        relative = f"{directory}/{entry.name}"
        safe = _safe_file(repo_root, relative)
        if safe is None:
            continue
        try:
            size = safe.stat().st_size
        except OSError:
            continue
        if size > _MAX_MARKDOWN_FILE_BYTES:
            item = {"repo_id": repo_id, "path": relative, "size": size}
            skipped.append(item)
            logger.warning("index_markdown_skipped_too_large repo_id=%s path=%s size=%d",
                           repo_id, relative, size)
            continue
        try:
            lines = safe.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        sections = [(index, line) for index, line in enumerate(lines)
                    if line.startswith("## ")]
        for number, (start, heading) in enumerate(sections, start=1):
            end = sections[number][0] if number < len(sections) else len(lines)
            body = "\n".join(lines[start + 1:end])[:4000]
            docs.append(_doc(repo_id, kind, f"{entry.name}#{number}", path=relative,
                             line=start + 1, title=heading[3:].strip(), body=body,
                             ts=None, confidential=confidential))
    return docs, skipped


def _repo_docs(conn: sqlite3.Connection, repo) -> tuple[list[dict], list[dict]]:
    repo_id = repo["repo_id"]
    repo_root = Path(repo["path"]).resolve()
    confidential = bool(repo["confidential"])
    docs = []
    for task in conn.execute(
        "SELECT task_id,goal,created_at,updated_at FROM tasks WHERE repo_id=? ORDER BY task_id", (repo_id,)
    ).fetchall():
        docs.append(_doc(repo_id, "task", task["task_id"], path=None, line=None,
                         title=task["task_id"], body=task["goal"], ts=task["updated_at"] or task["created_at"],
                         confidential=confidential))

    reviews = conn.execute(
        "SELECT review_id,task_id,findings_json,created_at,verdict,state,rowid AS ordinal FROM reviews WHERE task_id IN "
        "(SELECT task_id FROM tasks WHERE repo_id=?) ORDER BY rowid", (repo_id,)
    ).fetchall()
    for review_index, review in enumerate(reviews):
        try:
            findings = json.loads(review["findings_json"] or "[]")
        except json.JSONDecodeError:
            continue
        if not isinstance(findings, list):
            continue
        for finding in findings:
            if not isinstance(finding, dict) or not isinstance(finding.get("text"), str):
                continue
            finding_id = finding.get("id")
            if not isinstance(finding_id, str) or not finding_id:
                continue
            title = f"{finding_id} {finding.get('severity', '')}".strip()
            superseded_by = None
            if review["verdict"] == "REWORK_REQUIRED":
                superseded_by = next((later["review_id"] for later in reviews[review_index + 1:]
                                      if later["task_id"] == review["task_id"]
                                      and later["verdict"] in {"PASS", "PASS_WITH_NOTES"}), None)
            docs.append(_doc(repo_id, "finding", f"{review['review_id']}#{finding_id}",
                             path=finding.get("path") if isinstance(finding.get("path"), str) else None,
                             line=finding.get("line") if isinstance(finding.get("line"), int) else None,
                             title=title, body=finding["text"], ts=review["created_at"],
                             confidential=confidential, superseded_by=superseded_by))

    if repo["legacy_bridge"]:
        for record in conn.execute(
            "SELECT kind,key,ref,path,line,title,value,imported_at FROM legacy_records "
            "WHERE repo_id=? ORDER BY kind,key", (repo_id,)
        ).fetchall():
            kind, key = record["kind"], record["key"]
            path = record["path"]
            source = _safe_file(repo_root, path)
            if source is None:
                continue
            if kind == "decision":
                ref = record["ref"] or key.split("@L", 1)[0]
                body = _legacy_decision_body(source, record["line"] or 1, record["title"] or "")
                docs.append(_doc(repo_id, "decision", ref, path=path, line=record["line"],
                                 title=record["title"], body=body, ts=record["imported_at"],
                                 confidential=confidential))
            elif kind == "review":
                docs.append(_doc(repo_id, "review", key, path=path, line=record["line"],
                                 title=record["title"], body=record["title"] or "",
                                 ts=record["imported_at"], confidential=confidential))
            elif kind == "status":
                status_task = conn.execute(
                    "SELECT value FROM legacy_records WHERE repo_id=? AND kind='status' AND key='task'",
                    (repo_id,),
                ).fetchone()
                ref = status_task["value"] if status_task and status_task["value"] else key
                text = f"{key}: {record['value'] or ''}"
                docs.append(_doc(repo_id, "status", ref, path=path, line=record["line"],
                                 title=key, body=text, ts=record["imported_at"], confidential=confidential))

    docs.extend(_markdown_plan_docs(repo_id, repo_root, confidential))
    skipped_files = []
    for directory, kind in ((".ai-bridge/review", "review_section"),
                            (".ai-bridge/research", "research_section")):
        section_docs, skipped = _markdown_section_docs(repo_id, repo_root, directory, kind, confidential)
        docs.extend(section_docs)
        skipped_files.extend(skipped)

    result = gitio.run(repo_root, "log", "-n", "500", "--format=%H%x1f%s%x1f%b%x1f%cI%x1e",
                       read_only=True, check=False)
    if result.returncode:
        raise ValidationError("index_git_log_failed", detail={
            "repo_id": repo_id, "returncode": result.returncode,
            "stderr": result.stderr.decode("utf-8", "surrogateescape").strip(),
        })
    for record in result.stdout.decode("utf-8", "surrogateescape").split("\x1e"):
        fields = record.strip("\r\n").split("\x1f", 3)
        if len(fields) != 4 or not fields[0]:
            continue
        sha, subject, body, commit_ts = fields
        docs.append(_doc(repo_id, "commit", sha, path=None, line=None, title=subject,
                         body="\n".join(value for value in (subject, body) if value),
                         ts=commit_ts, confidential=confidential))
    return docs, skipped_files


def rebuild_index(conn: sqlite3.Connection, repo_id: str | None = None) -> dict:
    if repo_id is None:
        repos = conn.execute("SELECT * FROM repos ORDER BY repo_id").fetchall()
    else:
        repos = [repo_row(conn, repo_id)]
    totals = {"repos": 0, "indexed": 0, "updated": 0, "deleted": 0, "skipped": 0,
              "skipped_large": 0, "skipped_files": [], "total": 0}
    for repo in repos:
        docs, skipped_files = _repo_docs(conn, repo)
        totals["skipped_large"] += len(skipped_files)
        totals["skipped_files"].extend(skipped_files)
        desired = {doc["doc_id"]: doc for doc in docs}
        old_rows = conn.execute("SELECT * FROM search_docs WHERE repo_id=?", (repo["repo_id"],)).fetchall()
        old = {row["doc_id"]: row for row in old_rows}
        with tx(conn):
            for doc_id, doc in desired.items():
                previous = old.get(doc_id)
                if previous is not None and previous["content_hash"] == doc["content_hash"]:
                    totals["skipped"] += 1
                    continue
                if previous is not None:
                    conn.execute("DELETE FROM search_tri WHERE doc_id=?", (doc_id,))
                    conn.execute("DELETE FROM search_bi WHERE doc_id=?", (doc_id,))
                    totals["updated"] += 1
                else:
                    totals["indexed"] += 1
                conn.execute(
                    "INSERT INTO search_docs(doc_id,repo_id,kind,ref,path,line,title,body,ts,content_hash,superseded_by,confidential,indexed_at) "
                    "VALUES(:doc_id,:repo_id,:kind,:ref,:path,:line,:title,:body,:ts,:content_hash,:superseded_by,:confidential,:indexed_at) "
                    "ON CONFLICT(doc_id) DO UPDATE SET repo_id=excluded.repo_id,kind=excluded.kind,ref=excluded.ref,"
                    "path=excluded.path,line=excluded.line,title=excluded.title,body=excluded.body,ts=excluded.ts,"
                    "content_hash=excluded.content_hash,superseded_by=excluded.superseded_by,"
                    "confidential=excluded.confidential,indexed_at=excluded.indexed_at",
                    doc,
                )
                conn.execute("INSERT INTO search_tri(doc_id,text) VALUES(?,?)", (doc_id, doc["text"]))
                conn.execute("INSERT INTO search_bi(doc_id,text) VALUES(?,?)",
                             (doc_id, bigram_text(doc["text"])))
            for doc_id in old.keys() - desired.keys():
                conn.execute("DELETE FROM search_tri WHERE doc_id=?", (doc_id,))
                conn.execute("DELETE FROM search_bi WHERE doc_id=?", (doc_id,))
                conn.execute("DELETE FROM search_docs WHERE doc_id=?", (doc_id,))
                totals["deleted"] += 1
        totals["repos"] += 1
        totals["total"] += len(desired)
    return totals


def index_stats(conn: sqlite3.Connection) -> dict:
    counts = {row["kind"]: row["n"] for row in conn.execute(
        "SELECT kind,COUNT(*) AS n FROM search_docs GROUP BY kind ORDER BY kind"
    ).fetchall()}
    repos = conn.execute("SELECT COUNT(DISTINCT repo_id) FROM search_docs").fetchone()[0]
    return {"repos": repos, "documents": sum(counts.values()), "by_kind": counts}
