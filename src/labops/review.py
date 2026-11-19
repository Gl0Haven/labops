import json
import sqlite3

from .canon import canonical_json, sha256_hex
from .config import now
from .db import tx
from .errors import Blocked, IdempotencyConflict, IllegalTransition, ValidationError
from .ids import new_id, validate_actor
from .statemachine import transition
from .tasks import round_row, task_row

_VERDICTS = {"PASS", "PASS_WITH_NOTES", "REWORK_REQUIRED", "BLOCKED", "INCONCLUSIVE"}
_SEVERITIES = {"blocker", "major", "minor", "note"}


def open_review(conn: sqlite3.Connection, round_id: str, reviewer: str) -> dict:
    validate_actor(reviewer)
    rnd = round_row(conn, round_id)
    if rnd["state"] != "VERIFIED":
        raise Blocked("round_not_verified", detail={"round_id": round_id, "state": rnd["state"]})
    task = task_row(conn, rnd["task_id"])
    review_id = new_id("rv")
    timestamp = now()
    with tx(conn):
        conn.execute("INSERT INTO reviews(review_id,task_id,round_id,reviewer,state,verdict,independence,base_commit,reviewed_commit,reviewed_tree,scope_id,acceptance_profile_hash,findings_json,created_at,updated_at) "
                     "VALUES(?,?,?,?,'WAITING',NULL,NULL,?,?,?,?,?,NULL,?,?)",
                     (review_id, task["task_id"], round_id, reviewer, rnd["base_commit"], rnd["head_commit"],
                      rnd["head_tree"], task["policy_hash"] or "none", task["profile_hash"], timestamp, timestamp))
        conn.execute("INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
                     (timestamp, "review", review_id, None, "WAITING", reviewer,
                      canonical_json({"round_id": round_id, "reviewed_commit": rnd["head_commit"], "reviewed_tree": rnd["head_tree"]}).decode()))
    return {"review_id": review_id, "round_id": round_id, "reviewer": reviewer, "state": "WAITING",
            "reviewed_commit": rnd["head_commit"], "reviewed_tree": rnd["head_tree"]}


def _validate_findings(findings: list[dict]) -> None:
    if not isinstance(findings, list):
        raise ValidationError("invalid_findings")
    for i, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise ValidationError("invalid_finding", detail={"index": i})
        if not isinstance(finding.get("id"), str) or finding.get("severity") not in _SEVERITIES:
            raise ValidationError("invalid_finding", detail={"index": i, "field": "id/severity"})
        if finding.get("path") is not None and not isinstance(finding.get("path"), str):
            raise ValidationError("invalid_finding", detail={"index": i, "field": "path"})
        if finding.get("symbol") is not None and not isinstance(finding.get("symbol"), str):
            raise ValidationError("invalid_finding", detail={"index": i, "field": "symbol"})
        if not isinstance(finding.get("text"), str):
            raise ValidationError("invalid_finding", detail={"index": i, "field": "text"})


def submit_review(conn: sqlite3.Connection, *, review_id: str, actor: str, verdict: str,
                  findings: list[dict] | None, client_request_id: str, source: str = "cli") -> dict:
    validate_actor(actor)
    if source not in {"cli", "mcp"}:
        raise ValidationError("invalid_claim_source", detail={"source": source})
    if verdict not in _VERDICTS:
        raise ValidationError("invalid_review_verdict", detail={"verdict": verdict})
    findings = [] if findings is None else findings
    _validate_findings(findings)
    review = conn.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone()
    if review is None:
        raise ValidationError("review_not_found", detail={"review_id": review_id})
    payload = {"review_id": review_id, "verdict": verdict, "findings": findings}
    payload_json = canonical_json(payload).decode("utf-8")
    payload_hash = sha256_hex(payload_json.encode("utf-8"))
    with tx(conn):
        if actor != review["reviewer"]:
            raise ValidationError("reviewer_mismatch", detail={"actor": actor, "reviewer": review["reviewer"]})
        old_claim = conn.execute("SELECT * FROM claims WHERE round_id=? AND actor=? AND kind='submit_review' AND client_request_id=?",
                                 (review["round_id"], actor, client_request_id)).fetchone()
        if old_claim:
            if old_claim["payload_hash"] != payload_hash:
                raise IdempotencyConflict("request_id_payload_mismatch", detail={"claim_id": old_claim["claim_id"]})
            return {"review_id": review_id, "claim_id": old_claim["claim_id"], "state": review["state"], "replayed": True}
        if review["state"] != "WAITING":
            raise IllegalTransition("review_not_waiting", detail={"review_id": review_id, "state": review["state"]})
        rnd = round_row(conn, review["round_id"])
        if rnd["head_commit"] != review["reviewed_commit"]:
            transition(conn, "review", review_id, "STALE", actor="labops", detail={"reason": "reviewed_commit_changed"})
            raise_after = True
        else:
            raise_after = False
        if raise_after:
            # Commit the stale marker before returning the blocked result.
            pass
        else:
            claim_id = new_id("cl")
            timestamp = now()
            fingerprint = f"{review['task_id']}|{rnd['n']}|{actor}|{review['reviewed_commit']}"
            conn.execute("INSERT INTO claims(claim_id,round_id,actor,kind,client_request_id,payload_json,payload_hash,fingerprint,status,created_at,source) "
                         "VALUES(?,?,?,'submit_review',?,?,?,?, 'accepted',?,?)",
                         (claim_id, review["round_id"], actor, client_request_id, payload_json, payload_hash, fingerprint, timestamp, source))
            independence = "SELF_REVIEWED_FIX" if actor == rnd["actor"] else "INDEPENDENT"
            transition(conn, "review", review_id, verdict, actor=actor, detail={"claim_id": claim_id})
            conn.execute("UPDATE reviews SET verdict=?,independence=?,findings_json=?,claim_id=?,updated_at=? WHERE review_id=?",
                         (verdict, independence, canonical_json(findings).decode("utf-8"), claim_id, timestamp, review_id))
    if raise_after:
        raise Blocked("review_stale", detail={"review_id": review_id})
    return {"review_id": review_id, "claim_id": claim_id, "state": verdict, "independence": independence, "replayed": False}
