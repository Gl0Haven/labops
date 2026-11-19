import sqlite3

from .canon import canonical_json
from .config import now
from .errors import IllegalTransition, ValidationError

TRANSITIONS = {
    "task": {"CREATED": {"ACTIVE", "ABANDONED"}, "ACTIVE": {"READY_FOR_INTEGRATION", "ABANDONED"},
             "READY_FOR_INTEGRATION": {"ACTIVE", "INTEGRATED", "ABANDONED"}, "INTEGRATED": set(), "ABANDONED": set()},
    "round": {"OPEN": {"CLAIMED", "ABANDONED"}, "CLAIMED": {"VERIFYING"},
              "VERIFYING": {"VERIFIED", "REJECTED", "BLOCKED"}, "BLOCKED": {"VERIFYING"},
              "VERIFIED": set(), "REJECTED": set(), "ABANDONED": set()},
    "review": {"WAITING": {"PASS", "PASS_WITH_NOTES", "REWORK_REQUIRED", "BLOCKED", "INCONCLUSIVE", "STALE"},
               "PASS": {"STALE"}, "PASS_WITH_NOTES": {"STALE"}, "REWORK_REQUIRED": {"STALE"},
               "BLOCKED": {"STALE"}, "INCONCLUSIVE": {"STALE"}, "STALE": set()},
    "integration": {"CANDIDATE": {"CHECKED", "REJECTED", "STALE"}, "CHECKED": {"AWAITING_HUMAN_APPROVAL", "STALE"},
                    "AWAITING_HUMAN_APPROVAL": {"MERGED", "STALE", "REJECTED"},
                    "MERGED": set(), "STALE": set(), "REJECTED": set()},
    "operation": {"PENDING": {"APPLIED", "CONFLICT", "FAILED", "UNKNOWN"},
                  "APPLIED": set(), "CONFLICT": set(), "FAILED": set(), "UNKNOWN": set()},
}

_ENTITY = {
    "task": ("tasks", "task_id", "state"),
    "round": ("rounds", "round_id", "state"),
    "review": ("reviews", "review_id", "state"),
    "integration": ("integrations", "integration_id", "state"),
    "operation": ("operations", "op_id", "status"),
}


def transition(conn: sqlite3.Connection, entity_type: str, entity_id: str, to_state: str, *, actor: str, detail: dict | None = None) -> None:
    if entity_type not in _ENTITY:
        raise IllegalTransition("unknown_entity_type", detail={"entity_type": entity_type})
    table, key, state_col = _ENTITY[entity_type]
    row = conn.execute(f"SELECT {state_col} FROM {table} WHERE {key}=?", (entity_id,)).fetchone()
    if row is None:
        raise ValidationError("entity_not_found", detail={"entity_type": entity_type, "entity_id": entity_id})
    from_state = row[0]
    if to_state not in TRANSITIONS[entity_type].get(from_state, set()):
        raise IllegalTransition("illegal_transition", detail={"entity_type": entity_type, "entity_id": entity_id,
                                                               "from": from_state, "to": to_state})
    conn.execute(f"UPDATE {table} SET {state_col}=?, updated_at=? WHERE {key}=?", (to_state, now(), entity_id))
    conn.execute(
        "INSERT INTO events(ts, entity_type, entity_id, from_state, to_state, actor, detail_json) VALUES(?,?,?,?,?,?,?)",
        (now(), entity_type, entity_id, from_state, to_state, actor,
         canonical_json(detail).decode("utf-8") if detail is not None else None),
    )
