import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import gitio
from .canon import canonical_json
from .config import now
from .crash import crashpoint
from .db import tx
from .errors import ValidationError
from .ids import new_id
from .statemachine import transition


@dataclass(frozen=True)
class OpSpec:
    repo_id: str
    kind: str
    ref: str | None
    expected_old: str | None
    target: str
    idempotency_id: str
    on_applied: dict | None = None


ON_APPLIED: dict[str, Callable[[sqlite3.Connection, dict], None]] = {}


def register_on_applied(name: str, callback: Callable[[sqlite3.Connection, dict], None]) -> None:
    ON_APPLIED[name] = callback


def get_operation(conn: sqlite3.Connection, op_id: str):
    row = conn.execute("SELECT * FROM operations WHERE op_id=?", (op_id,)).fetchone()
    if row is None:
        raise ValidationError("operation_not_found", detail={"op_id": op_id})
    return row


def operation_by_idempotency(conn: sqlite3.Connection, idempotency_id: str):
    return conn.execute("SELECT * FROM operations WHERE idempotency_id=?", (idempotency_id,)).fetchone()


def _apply_callback(conn: sqlite3.Connection, encoded: str | None) -> None:
    if not encoded:
        return
    spec = json.loads(encoded)
    if spec is None:
        return
    callback_type = spec.get("type")
    callback = ON_APPLIED.get(callback_type)
    if callback is None:
        raise ValidationError("unknown_on_applied", detail={"type": callback_type})
    callback(conn, {k: v for k, v in spec.items() if k != "type"})


def _set_status(conn: sqlite3.Connection, op_id: str, status: str, *, detail: dict | None = None) -> None:
    if detail is not None:
        conn.execute("UPDATE operations SET detail_json=?, updated_at=? WHERE op_id=?",
                     (canonical_json(detail).decode("utf-8"), now(), op_id))
    transition(conn, "operation", op_id, status, actor="labops", detail=detail)


def apply_ref_op(conn: sqlite3.Connection, repo_path: Path, spec: OpSpec):
    if spec.ref is None:
        raise ValidationError("operation_ref_required", detail={"kind": spec.kind})
    with tx(conn):
        row = operation_by_idempotency(conn, spec.idempotency_id)
        if row is not None:
            return row
        op_id = new_id("op")
        timestamp = now()
        conn.execute(
            "INSERT INTO operations(op_id,repo_id,kind,ref,expected_old,target,status,idempotency_id,on_applied_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'PENDING',?,?,?,?)",
            (op_id, spec.repo_id, spec.kind, spec.ref, spec.expected_old, spec.target, spec.idempotency_id,
             canonical_json(spec.on_applied).decode("utf-8"), timestamp, timestamp),
        )
    crashpoint("op_after_intent")
    if gitio.object_type(repo_path, spec.target) != "commit":
        with tx(conn):
            _set_status(conn, op_id, "FAILED", detail={"reason": "target_not_commit"})
        return get_operation(conn, op_id)
    expected = spec.expected_old or gitio.zero_oid(repo_path)
    cas_ok = gitio.update_ref_cas(repo_path, spec.ref, spec.target, expected)
    crashpoint("op_after_git")
    return finalize_ref_op(conn, repo_path, op_id, cas_ok=cas_ok)


def finalize_ref_op(conn: sqlite3.Connection, repo_path: Path, op_id: str, cas_ok: bool | None):
    op = get_operation(conn, op_id)
    if op["ref"] is None:
        raise ValidationError("operation_ref_required", detail={"op_id": op_id})
    actual = gitio.read_ref(repo_path, op["ref"])
    with tx(conn):
        current = get_operation(conn, op_id)
        if current["status"] != "PENDING":
            return current
        if actual == current["target"]:
            _set_status(conn, op_id, "APPLIED")
            _apply_callback(conn, current["on_applied_json"])
        elif cas_ok is None and actual == current["expected_old"]:
            pass
        else:
            _set_status(conn, op_id, "CONFLICT", detail={"actual": actual})
    crashpoint("op_after_record")
    return get_operation(conn, op_id)
