import json
import os
import sqlite3
from pathlib import Path

from . import gitio
from .canon import canonical_json
from .config import home, now
from .db import tx
from .errors import ValidationError
from . import gate as _gate  # registers the integration_merged on_applied callback
from .journal import OpSpec, _apply_callback, apply_ref_op, finalize_ref_op, get_operation
from .locks import file_lock, try_lock
from .statemachine import transition
from .tasks import repo_lock, round_row, task_row


_COUNTERS = ("operations_applied", "operations_retried", "operations_failed", "operations_conflict",
             "operations_unknown", "verification_interrupted", "refs_repaired", "integrations_stale",
             "worktrees_removed", "integrity_conflicts", "unknown_refs", "unattributed_commits")


def _counts() -> dict[str, int]:
    return {name: 0 for name in _COUNTERS}


def _record_op(conn: sqlite3.Connection, op_id: str, status: str, *, detail: dict | None = None) -> None:
    with tx(conn):
        row = get_operation(conn, op_id)
        if row["status"] != "PENDING":
            return
        if detail is not None:
            conn.execute("UPDATE operations SET detail_json=?,updated_at=? WHERE op_id=?",
                         (canonical_json(detail).decode("utf-8"), now(), op_id))
        transition(conn, "operation", op_id, status, actor="reconcile", detail=detail)


def _apply_worktree(conn: sqlite3.Connection, repo_path: Path, op) -> str:
    spec = json.loads(op["on_applied_json"] or "null") or {}
    callback = spec
    branch = callback.get("branch")
    wt_path = Path(callback.get("path", ""))
    records = gitio.worktrees(repo_path)
    resolved = os.path.normcase(str(wt_path.resolve()))
    actual = next((item for item in records if os.path.normcase(str(Path(item["path"]).resolve())) == resolved), None)
    if actual and actual.get("branch") == f"refs/heads/{branch}":
        with tx(conn):
            current = get_operation(conn, op["op_id"])
            if current["status"] == "PENDING":
                transition(conn, "operation", op["op_id"], "APPLIED", actor="reconcile")
                _apply_callback(conn, current["on_applied_json"])
        return "APPLIED"
    if wt_path.exists():
        _record_op(conn, op["op_id"], "FAILED", detail={"reason": "path_exists"})
        return "FAILED"
    ref = f"refs/heads/{branch}"
    if gitio.read_ref(repo_path, ref) is not None:
        _record_op(conn, op["op_id"], "CONFLICT", detail={"reason": "branch_exists"})
        return "CONFLICT"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    result = gitio.run(repo_path, "worktree", "add", "-b", branch, str(wt_path), op["target"], read_only=False, check=False)
    records = gitio.worktrees(repo_path)
    actual = next((item for item in records if os.path.normcase(str(Path(item["path"]).resolve())) == resolved), None)
    if actual and actual.get("branch") == ref:
        with tx(conn):
            current = get_operation(conn, op["op_id"])
            if current["status"] == "PENDING":
                transition(conn, "operation", op["op_id"], "APPLIED", actor="reconcile", detail={"retried": True})
                _apply_callback(conn, current["on_applied_json"])
        return "APPLIED"
    stderr = result.stderr.decode("utf-8", "surrogateescape")
    if gitio.read_ref(repo_path, ref) is not None:
        _record_op(conn, op["op_id"], "CONFLICT", detail={"reason": "branch_exists", "stderr": stderr})
        return "CONFLICT"
    _record_op(conn, op["op_id"], "FAILED", detail={"reason": "worktree_create_failed", "stderr": stderr})
    return "FAILED"


def _snapshot_round_id(op) -> str | None:
    parts = (op["idempotency_id"] or "").split(":")
    return parts[1] if len(parts) == 4 and parts[0] == "snap" else None


def _reconcile_snapshot(conn: sqlite3.Connection, repo_path: Path, op) -> str:
    rid = _snapshot_round_id(op)
    if rid is None:
        _record_op(conn, op["op_id"], "UNKNOWN", detail={"reason": "snapshot_round_unknown"})
        return "UNKNOWN"
    rnd = round_row(conn, rid)
    wt = Path(rnd["worktree_path"]) if rnd["worktree_path"] else None
    if wt is None:
        _record_op(conn, op["op_id"], "UNKNOWN", detail={"reason": "snapshot_worktree_missing"})
        return "UNKNOWN"
    head = gitio.rev_parse(wt, "HEAD")
    if head == op["expected_old"]:
        _record_op(conn, op["op_id"], "FAILED", detail={"reason": "interrupted_before_commit"})
        return "FAILED"
    parent = gitio.rev_parse(wt, f"{head}^") if head else None
    trailers = gitio.commit_trailers(wt, head) if head else {}
    task = task_row(conn, rnd["task_id"])
    expected = {"LabOps-Task": task["task_id"], "LabOps-Round": str(rnd["n"]), "LabOps-Actor": rnd["actor"]}
    if head and parent == op["expected_old"] and all(trailers.get(k) == v for k, v in expected.items()):
        with tx(conn):
            current = get_operation(conn, op["op_id"])
            if current["status"] == "PENDING":
                conn.execute("UPDATE operations SET target=?,updated_at=? WHERE op_id=?", (head, now(), op["op_id"]))
                transition(conn, "operation", op["op_id"], "APPLIED", actor="reconcile", detail={"target": head})
        return "APPLIED"
    _record_op(conn, op["op_id"], "UNKNOWN", detail={"reason": "snapshot_state_ambiguous", "head": head, "parent": parent})
    return "UNKNOWN"


def _reconcile_ff(conn: sqlite3.Connection, repo_path: Path, repo, op) -> str:
    actual = gitio.read_ref(repo_path, op["ref"])
    callback = json.loads(op["on_applied_json"] or "{}")
    if actual == op["target"]:
        target_tree = callback.get("candidate_tree")
        if target_tree is not None and gitio.tree_of(repo_path, actual) != target_tree:
            _record_op(conn, op["op_id"], "CONFLICT", detail={"reason": "main_tree_mismatch", "actual": actual})
            return "CONFLICT"
        with tx(conn):
            current = get_operation(conn, op["op_id"])
            if current["status"] == "PENDING":
                transition(conn, "operation", op["op_id"], "APPLIED", actor="reconcile")
                _apply_callback(conn, current["on_applied_json"])
        return "APPLIED"
    if actual == op["expected_old"]:
        wt = next((item for item in gitio.worktrees(repo_path) if item.get("branch") == repo["main_ref"]), None)
        dirty = bool(wt and gitio.status_porcelain(Path(wt["path"]), untracked="no"))
        if dirty:
            _record_op(conn, op["op_id"], "CONFLICT", detail={"reason": "partial_merge_suspected", "actual": actual})
            return "CONFLICT"
        _record_op(conn, op["op_id"], "FAILED", detail={"reason": "merge_not_applied"})
        return "FAILED"
    _record_op(conn, op["op_id"], "CONFLICT", detail={"actual": actual})
    return "CONFLICT"


def _pending_operations(conn: sqlite3.Connection, repo_path: Path, repo, counts: dict) -> None:
    rows = conn.execute("SELECT * FROM operations WHERE repo_id=? AND status='PENDING' ORDER BY created_at,op_id",
                        (repo["repo_id"],)).fetchall()
    for op in rows:
        if op["kind"] == "create_ref":
            actual = gitio.read_ref(repo_path, op["ref"])
            if actual == op["target"]:
                result = finalize_ref_op(conn, repo_path, op["op_id"], cas_ok=None)["status"]
            elif actual is None:
                with tx(conn):
                    conn.execute("UPDATE operations SET detail_json=?,updated_at=? WHERE op_id=?",
                                 (canonical_json({"retried": True}).decode(), now(), op["op_id"]))
                expected = op["expected_old"] or gitio.zero_oid(repo_path)
                cas_ok = gitio.update_ref_cas(repo_path, op["ref"], op["target"], expected)
                result = finalize_ref_op(conn, repo_path, op["op_id"], cas_ok=cas_ok)["status"]
                counts["operations_retried"] += 1
            else:
                _record_op(conn, op["op_id"], "CONFLICT", detail={"actual": actual})
                result = "CONFLICT"
            if result == "CONFLICT" and (op["idempotency_id"] or "").startswith("submit:"):
                with tx(conn):
                    conn.execute("UPDATE claims SET status='rejected',reject_reason='ref_conflict' WHERE claim_id=? AND status='received'",
                                 (op["idempotency_id"].split(":", 1)[1],))
        elif op["kind"] == "create_worktree":
            result = _apply_worktree(conn, repo_path, op)
        elif op["kind"] == "snapshot_commit":
            result = _reconcile_snapshot(conn, repo_path, op)
        elif op["kind"] == "ff_main":
            result = _reconcile_ff(conn, repo_path, repo, op)
        else:
            _record_op(conn, op["op_id"], "UNKNOWN", detail={"reason": "unknown_operation_kind"})
            result = "UNKNOWN"
        if result == "APPLIED":
            counts["operations_applied"] += 1
        elif result == "FAILED":
            counts["operations_failed"] += 1
        elif result == "CONFLICT":
            counts["operations_conflict"] += 1
        elif result == "UNKNOWN":
            counts["operations_unknown"] += 1


def _interrupt_verifications(conn: sqlite3.Connection, repo_id: str, counts: dict) -> None:
    rounds = conn.execute("SELECT r.* FROM rounds r JOIN tasks t ON t.task_id=r.task_id WHERE t.repo_id=? AND r.state='VERIFYING' ORDER BY r.created_at",
                          (repo_id,)).fetchall()
    for rnd in rounds:
        with try_lock(home() / "locks" / f"verify-{rnd['round_id'].replace('/', '_')}.lock") as acquired:
            if not acquired:
                continue
            with tx(conn):
                current = round_row(conn, rnd["round_id"])
                if current["state"] != "VERIFYING":
                    continue
                evidence = conn.execute("SELECT evidence_id FROM evidence WHERE round_id=? AND status='running'",
                                        (rnd["round_id"],)).fetchall()
                for row in evidence:
                    conn.execute("UPDATE evidence SET status='interrupted',finished_at=? WHERE evidence_id=?",
                                 (now(), row["evidence_id"]))
                transition(conn, "round", rnd["round_id"], "BLOCKED", actor="reconcile",
                           detail={"reason": "verify_interrupted"})
                counts["verification_interrupted"] += 1


def _check_round_refs(conn: sqlite3.Connection, repo_id: str, repo_path: Path, counts: dict) -> set[str]:
    known = {row[0] for row in conn.execute("SELECT ref FROM operations WHERE repo_id=? AND ref IS NOT NULL", (repo_id,))}
    rounds = conn.execute("SELECT r.* FROM rounds r JOIN tasks t ON t.task_id=r.task_id WHERE t.repo_id=? AND r.head_commit IS NOT NULL",
                          (repo_id,)).fetchall()
    for rnd in rounds:
        ref = f"refs/labops/{rnd['task_id']}/r{rnd['n']}/{rnd['actor']}/submitted"
        known.add(ref)
        actual = gitio.read_ref(repo_path, ref)
        if actual is None:
            if gitio.object_type(repo_path, rnd["head_commit"]) == "commit":
                # Opus review fix: each repair needs a fresh idempotency id, otherwise a second deletion of
                # the same ref replays the first APPLIED op and the ref is never restored.
                prior = conn.execute("SELECT COUNT(*) FROM operations WHERE idempotency_id=? OR idempotency_id GLOB ?",
                                     (f"repair:{rnd['round_id']}", f"repair:{rnd['round_id']}:*")).fetchone()[0]
                idem = f"repair:{rnd['round_id']}" if prior == 0 else f"repair:{rnd['round_id']}:{prior + 1}"
                spec = OpSpec(repo_id, "create_ref", ref, None, rnd["head_commit"], idem, None)
                repaired = apply_ref_op(conn, repo_path, spec)
                if repaired["status"] == "APPLIED":
                    counts["refs_repaired"] += 1
            continue
        if actual != rnd["head_commit"]:
            detail = {"reason": "integrity_conflict", "ref": ref, "expected": rnd["head_commit"], "actual": actual}
            marker = canonical_json(detail).decode("utf-8")
            prior = conn.execute("SELECT 1 FROM events WHERE entity_type='round' AND entity_id=? AND to_state='integrity_conflict' AND detail_json=? LIMIT 1",
                                 (rnd["round_id"], marker)).fetchone()
            if prior is None:
                with tx(conn):
                    conn.execute("INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
                                 (now(), "round", rnd["round_id"], None, "integrity_conflict", "reconcile", marker))
                counts["integrity_conflicts"] += 1
    return known


def _stale_integrations(conn: sqlite3.Connection, repo, repo_path: Path, counts: dict) -> None:
    rows = conn.execute("SELECT * FROM integrations WHERE task_id IN (SELECT task_id FROM tasks WHERE repo_id=?) "
                        "AND state IN ('CANDIDATE','CHECKED','AWAITING_HUMAN_APPROVAL') ORDER BY created_at",
                        (repo["repo_id"],)).fetchall()
    main = gitio.read_ref(repo_path, repo["main_ref"])
    for integration in rows:
        if main != integration["main_before"]:
            with tx(conn):
                current = conn.execute("SELECT state FROM integrations WHERE integration_id=?", (integration["integration_id"],)).fetchone()
                if current and current[0] in {"CANDIDATE", "CHECKED", "AWAITING_HUMAN_APPROVAL"}:
                    transition(conn, "integration", integration["integration_id"], "STALE", actor="reconcile",
                               detail={"reason": "main_advanced"})
                    counts["integrations_stale"] += 1


def _cleanup_verify_worktrees(conn: sqlite3.Connection, repo, repo_path: Path, counts: dict) -> None:
    verify_root = home() / "verify"
    if verify_root.is_dir():
        for path in sorted(verify_root.iterdir()):
            if not path.is_dir():
                continue
            evidence = conn.execute("SELECT status FROM evidence WHERE evidence_id=?", (path.name,)).fetchone()
            if evidence is not None and evidence[0] == "running":
                continue
            resolved = os.path.normcase(str(path.resolve()))
            registered = any(os.path.normcase(str(Path(item["path"]).resolve())) == resolved
                             for item in gitio.worktrees(repo_path))
            if not registered:
                continue
            result = gitio.run(repo_path, "worktree", "remove", "--force", str(path), read_only=False, check=False)
            if result.returncode == 0:
                counts["worktrees_removed"] += 1
    gitio.run(repo_path, "worktree", "prune", read_only=False, check=False)


def _unknown_refs(conn: sqlite3.Connection, repo_id: str, repo_path: Path, known: set[str], counts: dict) -> None:
    for ref, oid in gitio.for_each_ref(repo_path, "refs/labops/"):
        if ref in known:
            continue
        detail = {"ref": ref, "oid": oid}
        marker = canonical_json(detail).decode("utf-8")
        prior = conn.execute("SELECT 1 FROM events WHERE entity_type='repo' AND entity_id=? AND to_state='unknown_labops_ref' AND detail_json=? LIMIT 1",
                             (repo_id, marker)).fetchone()
        if prior is None:
            with tx(conn):
                conn.execute("INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
                             (now(), "repo", repo_id, None, "unknown_labops_ref", "reconcile", marker))
            counts["unknown_refs"] += 1


def _unattributed_commits(conn: sqlite3.Connection, repo_id: str, repo_path: Path, counts: dict) -> None:
    rounds = conn.execute(
        "SELECT r.* FROM rounds r JOIN tasks t ON t.task_id=r.task_id "
        "WHERE t.repo_id=? AND r.branch IS NOT NULL AND r.branch<>'' AND r.state IN ('OPEN','CLAIMED') "
        "ORDER BY r.created_at,r.round_id",
        (repo_id,),
    ).fetchall()
    for rnd in rounds:
        tip = gitio.read_ref(repo_path, f"refs/heads/{rnd['branch']}")
        expected = rnd["base_commit"] if rnd["state"] == "OPEN" else rnd["head_commit"]
        attributed = {row["target"] for row in conn.execute(
            "SELECT target FROM operations WHERE kind='snapshot_commit' AND status='APPLIED' "
            "AND idempotency_id GLOB ?", (f"snap:{rnd['round_id']}:*",)) if row["target"]}
        if tip is None or tip == expected or tip in attributed:
            continue
        detail = {"tip": tip, "expected": expected}
        marker = canonical_json(detail).decode("utf-8")
        prior = conn.execute(
            "SELECT 1 FROM events WHERE entity_type='round' AND entity_id=? "
            "AND to_state='unattributed_commit' AND detail_json=? LIMIT 1",
            (rnd["round_id"], marker),
        ).fetchone()
        if prior is None:
            with tx(conn):
                conn.execute(
                    "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (now(), "round", rnd["round_id"], None, "unattributed_commit", "reconcile", marker),
                )
            counts["unattributed_commits"] += 1


def reconcile(conn: sqlite3.Connection, repo_id: str | None = None) -> dict:
    counts = _counts()
    repos = conn.execute("SELECT * FROM repos WHERE repo_id=? ORDER BY repo_id" if repo_id else "SELECT * FROM repos ORDER BY repo_id",
                         (repo_id,) if repo_id else ()).fetchall()
    if repo_id and not repos:
        raise ValidationError("repo_not_found", detail={"repo_id": repo_id})
    for repo in repos:
        repo_path = Path(repo["path"])
        with file_lock(repo_lock(repo["repo_id"])):
            _pending_operations(conn, repo_path, repo, counts)
            _interrupt_verifications(conn, repo["repo_id"], counts)
            known = _check_round_refs(conn, repo["repo_id"], repo_path, counts)
            _stale_integrations(conn, repo, repo_path, counts)
            _cleanup_verify_worktrees(conn, repo, repo_path, counts)
            _unknown_refs(conn, repo["repo_id"], repo_path, known, counts)
            _unattributed_commits(conn, repo["repo_id"], repo_path, counts)
    return counts
