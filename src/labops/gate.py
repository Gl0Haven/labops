import json
import ast
import sqlite3
from pathlib import Path

from . import gitio
from .approvals import _insert_approval, require_human
from .canon import canonical_json
from .config import now
from .db import tx
from .errors import Blocked, GitConflict, ValidationError
from .ids import new_id
from .journal import get_operation, register_on_applied
from .locks import file_lock
from .policy import frozen_hits, out_of_scope
from .statemachine import transition
from .tasks import repo_lock, repo_row, round_row, task_row


def _task_policy(conn, task, repo):
    if task["policy_json"] is not None:
        return json.loads(task["policy_json"])
    event = conn.execute("SELECT detail_json FROM events WHERE entity_type='task' AND entity_id=? AND to_state='CREATED' ORDER BY seq LIMIT 1",
                         (task["task_id"],)).fetchone()
    if event:
        detail = json.loads(event[0] or "{}")
        if detail.get("frozen_policy") is not None or task["policy_hash"] is None:
            return detail.get("frozen_policy") or {"schema": 1, "frozen": []}
    if repo["policy_hash"] == task["policy_hash"] and repo["policy_json"]:
        return json.loads(repo["policy_json"])
    if task["policy_hash"] is None:
        return {"schema": 1, "frozen": []}
    raise ValidationError("frozen_policy_snapshot_missing", detail={"task_id": task["task_id"], "policy_hash": task["policy_hash"]})


def _latest_round(conn, task_id: str):
    return conn.execute("SELECT * FROM rounds WHERE task_id=? AND head_commit IS NOT NULL ORDER BY n DESC LIMIT 1", (task_id,)).fetchone()


def evaluate(conn: sqlite3.Connection, task_id: str) -> dict:
    task = task_row(conn, task_id)
    repo = repo_row(conn, task["repo_id"])
    repo_path = Path(repo["path"])
    profile_row = conn.execute("SELECT content_json FROM acceptance_profiles WHERE profile_hash=?", (task["profile_hash"],)).fetchone()
    if profile_row is None:
        raise ValidationError("profile_not_found", detail={"profile_hash": task["profile_hash"]})
    profile = json.loads(profile_row[0])
    policy = _task_policy(conn, task, repo)
    latest = _latest_round(conn, task_id)
    reasons: list[str] = []
    detail: dict = {"hits": [], "out": [], "ev_id": None, "review_ids": [],
                    "limits": "AST mapping is not a completeness proof"}
    if latest is None:
        reasons.append("no_submitted_round")
    else:
        detail["round_id"] = latest["round_id"]
        detail["head_commit"] = latest["head_commit"]
        detail["head_tree"] = latest["head_tree"]
        if latest["state"] != "VERIFIED":
            reasons.append(f"round_not_verified:{latest['state']}")
        evidence = conn.execute("SELECT * FROM evidence WHERE round_id=? AND kind='INDEPENDENTLY_EXECUTED' AND status='passed' "
                                "AND commit_oid=? AND tree_oid=? AND profile_hash=? ORDER BY created_at DESC LIMIT 1",
                                (latest["round_id"], latest["head_commit"], latest["head_tree"], task["profile_hash"])).fetchone()
        if evidence is None:
            reasons.append("no_independent_evidence")
        else:
            detail["ev_id"] = evidence["evidence_id"]
            # Passed evidence must record at least one command that actually ran and exited 0;
            # this also rejects rows written before verify refused empty profiles.
            try:
                results = json.loads(evidence["results_json"] or "[]")
            except (TypeError, ValueError):
                results = []
            executed = [item for item in results if isinstance(item, dict)
                        and not item.get("skipped") and item.get("exit_code") == 0]
            if not executed:
                reasons.append("evidence_without_checks")
        passes = conn.execute("SELECT * FROM reviews WHERE round_id=? AND state IN ('PASS','PASS_WITH_NOTES') "
                              "AND reviewed_tree=? AND acceptance_profile_hash=? ORDER BY created_at",
                              (latest["round_id"], latest["head_tree"], task["profile_hash"])).fetchall()
        detail["review_ids"] = [row["review_id"] for row in passes]
        if not passes:
            reasons.append("no_pass_review")
        else:
            independent = any(row["independence"] == "INDEPENDENT" for row in passes)
            accepted_self = any(row["independence"] == "SELF_REVIEWED_FIX" for row in passes) and conn.execute(
                "SELECT 1 FROM approvals WHERE task_id=? AND kind='accept_self_review' AND target_tree=? AND consumed_at IS NULL LIMIT 1",
                (task_id, latest["head_tree"])).fetchone() is not None
            if not independent and not accepted_self:
                reasons.append("self_review_needs_acceptance")
        # After sync_main the head contains other tasks' already-integrated changes, so diffing from
        # task.base_commit would attribute them to this task. When the current main tip is an
        # ancestor of the head (and descends from the task base), only main..head is this task's work.
        main_tip = gitio.read_ref(repo_path, repo["main_ref"])
        scope_base = task["base_commit"]
        if (main_tip and main_tip != task["base_commit"]
                and gitio.is_ancestor(repo_path, task["base_commit"], main_tip)
                and gitio.is_ancestor(repo_path, main_tip, latest["head_commit"])):
            scope_base = main_tip
        detail["scope_base"] = scope_base
        detail["hits"] = frozen_hits(repo_path, policy, scope_base, latest["head_commit"])
        if detail["hits"]:
            approved = conn.execute("SELECT 1 FROM approvals WHERE task_id=? AND kind='frozen_change' AND target_commit=? AND consumed_at IS NULL LIMIT 1",
                                    (task_id, latest["head_commit"])).fetchone()
            if not approved:
                reasons.append("frozen_zone_touched")
        detail["out"] = out_of_scope(repo_path, profile.get("allowed_paths", []), scope_base, latest["head_commit"])
        if detail["out"]:
            reasons.append("out_of_scope")
        blocking = conn.execute("SELECT review_id,state FROM reviews WHERE round_id=? AND state IN ('BLOCKED','REWORK_REQUIRED')",
                               (latest["round_id"],)).fetchall()
        detail["blocking_review_ids"] = [row["review_id"] for row in blocking]
        if blocking:
            reasons.append("open_blocking_review")
        ref = f"refs/labops/{task_id}/r{latest['n']}/{latest['actor']}/submitted"
        actual_ref = gitio.read_ref(repo_path, ref)
        detail["submitted_ref"] = {"name": ref, "actual": actual_ref, "expected": latest["head_commit"]}
        if actual_ref != latest["head_commit"]:
            reasons.append("submitted_ref_mismatch")
    passed = not reasons
    return {"pass": passed, "reasons": reasons, "detail": detail}


def gate_check(conn: sqlite3.Connection, task_id: str) -> dict:
    result = evaluate(conn, task_id)
    with tx(conn):
        current = task_row(conn, task_id)
        if current["state"] in {"ACTIVE", "READY_FOR_INTEGRATION"}:
            desired = "READY_FOR_INTEGRATION" if result["pass"] else "ACTIVE"
            if current["state"] != desired:
                transition(conn, "task", task_id, desired, actor="labops", detail={"reason": "gate_check", "reasons": result["reasons"]})
    return result


def _integration_merged(conn: sqlite3.Connection, params: dict) -> None:
    integration_id = params["integration_id"]
    integration = conn.execute("SELECT * FROM integrations WHERE integration_id=?", (integration_id,)).fetchone()
    if integration is None:
        raise ValidationError("integration_not_found", detail={"integration_id": integration_id})
    if integration["state"] == "MERGED":
        return
    transition(conn, "integration", integration_id, "MERGED", actor="labops", detail={"candidate_commit": params["candidate_commit"]})
    task = task_row(conn, params["task_id"])
    if task["state"] == "READY_FOR_INTEGRATION":
        transition(conn, "task", params["task_id"], "INTEGRATED", actor="labops", detail={"integration_id": integration_id})
    conn.execute("UPDATE approvals SET consumed_at=? WHERE task_id=? AND target_commit=? AND target_tree=? AND consumed_at IS NULL",
                 (now(), params["task_id"], params["candidate_commit"], params["candidate_tree"]))


register_on_applied("integration_merged", _integration_merged)


def _integration(conn, integration_id: str):
    row = conn.execute("SELECT * FROM integrations WHERE integration_id=?", (integration_id,)).fetchone()
    if row is None:
        raise ValidationError("integration_not_found", detail={"integration_id": integration_id})
    return row


def _tool_registry_at(repo_path: Path, revision: str) -> dict[str, tuple[str, ...]] | None:
    result = gitio.run(repo_path, "show", f"{revision}:src/labops/mcp_worker.py", check=False)
    if result.returncode:
        return None
    try:
        module = ast.parse(result.stdout.decode("utf-8", "surrogateescape"))
    except (SyntaxError, UnicodeError):
        return None
    value = None
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "TOOLS" for target in statement.targets
        ):
            value = statement.value
            break
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.target.id == "TOOLS":
            value = statement.value
            break
    if value is None:
        return None
    try:
        registry = ast.literal_eval(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(registry, dict):
        return None
    normalized = {}
    for name, parameters in registry.items():
        if (not isinstance(name, str) or not isinstance(parameters, (tuple, list))
                or any(not isinstance(parameter, str) for parameter in parameters)):
            return None
        normalized[name] = tuple(parameters)
    return normalized


def client_restart_requirement(repo_path: Path, main_before: str, candidate: str) -> dict:
    changed = gitio.run(repo_path, "diff", "--name-only", main_before, candidate, "--",
                        "src/labops/mcp_server.py", "src/labops/mcp_worker.py").stdout.decode(
                            "utf-8", "surrogateescape").splitlines()
    changed_set = {path.replace("\\", "/") for path in changed}
    if "src/labops/mcp_server.py" in changed_set:
        return {"client_restart_required": True, "reason": "mcp_server_changed"}
    if "src/labops/mcp_worker.py" not in changed_set:
        return {"client_restart_required": False, "reason": "no_mcp_tool_surface_change"}
    before = _tool_registry_at(repo_path, main_before)
    after = _tool_registry_at(repo_path, candidate)
    if before is None or after is None:
        return {"client_restart_required": True, "reason": "tool_registry_unavailable"}
    if before != after:
        return {"client_restart_required": True, "reason": "tool_registry_changed"}
    return {"client_restart_required": False, "reason": "worker_implementation_only"}


def propose_integration(conn: sqlite3.Connection, task_id: str) -> dict:
    task = task_row(conn, task_id)
    if task["state"] != "READY_FOR_INTEGRATION":
        raise Blocked("task_not_ready_for_integration", detail={"state": task["state"]})
    gate = gate_check(conn, task_id)
    if not gate["pass"]:
        raise Blocked("gate_failed", detail=gate)
    repo = repo_row(conn, task["repo_id"])
    repo_path = Path(repo["path"])
    latest = _latest_round(conn, task_id)
    main_before = gitio.read_ref(repo_path, repo["main_ref"])
    if main_before is None:
        raise ValidationError("main_ref_not_found", detail={"main_ref": repo["main_ref"]})
    integration_id = new_id("ig")
    timestamp = now()
    with tx(conn):
        conn.execute("INSERT INTO integrations(integration_id,task_id,candidate_commit,candidate_tree,main_before,state,check_json,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,'CANDIDATE',?,?,?)",
                     (integration_id, task_id, latest["head_commit"], latest["head_tree"], main_before,
                      canonical_json(gate).decode(), timestamp, timestamp))
        conn.execute("INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
                     (timestamp, "integration", integration_id, None, "CANDIDATE", "labops", canonical_json(gate).decode()))
        if gitio.is_ancestor(repo_path, main_before, latest["head_commit"]):
            transition(conn, "integration", integration_id, "CHECKED", actor="labops", detail={"main_before": main_before})
            transition(conn, "integration", integration_id, "AWAITING_HUMAN_APPROVAL", actor="labops")
        else:
            transition(conn, "integration", integration_id, "STALE", actor="labops", detail={"reason": "main_advanced"})
    row = _integration(conn, integration_id)
    result = {"integration_id": integration_id, "state": row["state"], "candidate_commit": row["candidate_commit"],
              "candidate_tree": row["candidate_tree"], "main_before": main_before}
    if row["state"] == "STALE":
        result["hint"] = "main advanced: start_work(role=fix), sync_main(round_id), submit_changes, review, then propose again"
    return result


def _insert_operation(conn, *, repo_id: str, integration_id: str, approval_id: str, main_ref: str,
                      main_before: str, candidate: str, candidate_tree: str, task_id: str) -> str:
    op_id = new_id("op")
    timestamp = now()
    on_applied = {"type": "integration_merged", "integration_id": integration_id, "approval_id": approval_id,
                  "task_id": task_id, "candidate_commit": candidate, "candidate_tree": candidate_tree}
    conn.execute("INSERT INTO operations(op_id,repo_id,kind,ref,expected_old,target,status,idempotency_id,on_applied_json,created_at,updated_at) "
                 "VALUES(?,?,'ff_main',?,?,?,'PENDING',?,?,?,?)",
                 (op_id, repo_id, main_ref, main_before, candidate, f"ff:{integration_id}:{approval_id}",
                  canonical_json(on_applied).decode(), timestamp, timestamp))
    return op_id


def _finish_ff(conn, repo_path: Path, op_id: str, main_ref: str):
    op = get_operation(conn, op_id)
    actual = gitio.read_ref(repo_path, main_ref)
    if actual == op["target"] and gitio.tree_of(repo_path, actual) == gitio.tree_of(repo_path, op["target"]):
        with tx(conn):
            current = get_operation(conn, op_id)
            if current["status"] == "PENDING":
                transition(conn, "operation", op_id, "APPLIED", actor="labops")
                from .journal import _apply_callback
                _apply_callback(conn, current["on_applied_json"])
    elif actual == op["expected_old"]:
        with tx(conn):
            current = get_operation(conn, op_id)
            if current["status"] == "PENDING":
                detail = {"reason": "merge_not_applied"}
                conn.execute("UPDATE operations SET detail_json=?,updated_at=? WHERE op_id=?",
                             (canonical_json(detail).decode(), now(), op_id))
                transition(conn, "operation", op_id, "FAILED", actor="labops", detail=detail)
    else:
        with tx(conn):
            current = get_operation(conn, op_id)
            if current["status"] == "PENDING":
                detail = {"actual": actual}
                conn.execute("UPDATE operations SET detail_json=?,updated_at=? WHERE op_id=?",
                             (canonical_json(detail).decode(), now(), op_id))
                transition(conn, "operation", op_id, "CONFLICT", actor="labops", detail=detail)
    return get_operation(conn, op_id)


def approve_integration(conn: sqlite3.Connection, integration_id: str) -> dict:
    integration = _integration(conn, integration_id)
    if integration["state"] != "AWAITING_HUMAN_APPROVAL":
        raise Blocked("integration_not_awaiting_approval", detail={"state": integration["state"]})
    task = task_row(conn, integration["task_id"])
    repo = repo_row(conn, task["repo_id"])
    repo_path = Path(repo["path"])
    if gitio.read_ref(repo_path, repo["main_ref"]) != integration["main_before"]:
        with tx(conn):
            transition(conn, "integration", integration_id, "STALE", actor="labops", detail={"reason": "main_advanced"})
        return {"integration_id": integration_id, "state": "STALE", "reason": "main_advanced"}
    grant = require_human(f"Approve fast-forward integration for tree {integration['candidate_tree'][:12]}? Type the tree prefix:",
                          integration["candidate_tree"][:12])
    with file_lock(repo_lock(repo["repo_id"])):
        integration = _integration(conn, integration_id)
        if integration["state"] != "AWAITING_HUMAN_APPROVAL":
            raise Blocked("integration_not_awaiting_approval", detail={"state": integration["state"]})
        # Opus review fix: the task may have moved on between propose and approve (new round,
        # new blocking review, revoked approval). Re-check under the repo lock and refuse to merge
        # a candidate that is no longer the gate-passing head.
        latest = _latest_round(conn, task["task_id"])
        task_now = task_row(conn, task["task_id"])
        stale_reason = None
        if task_now["state"] != "READY_FOR_INTEGRATION" or latest is None \
                or latest["head_commit"] != integration["candidate_commit"]:
            stale_reason = "task_changed_since_proposal"
        else:
            regate = gate_check(conn, task["task_id"])
            if not regate["pass"]:
                stale_reason = "gate_failed_at_approval"
        if stale_reason is None and gitio.read_ref(repo_path, repo["main_ref"]) != integration["main_before"]:
            stale_reason = "main_advanced"
        if stale_reason is not None:
            with tx(conn):
                transition(conn, "integration", integration_id, "STALE", actor="labops", detail={"reason": stale_reason})
            return {"integration_id": integration_id, "state": "STALE", "reason": stale_reason}
        worktree = next((w for w in gitio.worktrees(repo_path) if w.get("branch") == repo["main_ref"]), None)
        if worktree is not None and gitio.status_porcelain(Path(worktree["path"]), untracked="no"):
            raise Blocked("main_worktree_dirty", detail={"path": worktree["path"]})
        restart_info = client_restart_requirement(repo_path, integration["main_before"],
                                                  integration["candidate_commit"])
        with tx(conn):
            approval = _insert_approval(conn, task_id=task["task_id"], kind="integrate",
                                        commit=integration["candidate_commit"], tree=integration["candidate_tree"],
                                        granted_via=grant, integration_id=integration_id)
        op_id = None
        if worktree is not None:
            wt_path = Path(worktree["path"])
            with tx(conn):
                op_id = _insert_operation(conn, repo_id=repo["repo_id"], integration_id=integration_id,
                                          approval_id=approval["approval_id"], main_ref=repo["main_ref"],
                                          main_before=integration["main_before"], candidate=integration["candidate_commit"],
                                          candidate_tree=integration["candidate_tree"], task_id=task["task_id"])
            from .crash import crashpoint
            crashpoint("ff_after_intent")
            merge = gitio.run(wt_path, "merge", "--ff-only", integration["candidate_commit"], read_only=False, check=False)
            crashpoint("ff_after_merge")
            result = _finish_ff(conn, repo_path, op_id, repo["main_ref"])
            return {"integration_id": integration_id, "approval_id": approval["approval_id"], "op_id": op_id,
                    "state": "MERGED" if result["status"] == "APPLIED" else result["status"],
                    "merge_returncode": merge.returncode, **restart_info}
        with tx(conn):
            op_id = _insert_operation(conn, repo_id=repo["repo_id"], integration_id=integration_id,
                                      approval_id=approval["approval_id"], main_ref=repo["main_ref"],
                                      main_before=integration["main_before"], candidate=integration["candidate_commit"],
                                      candidate_tree=integration["candidate_tree"], task_id=task["task_id"])
        from .crash import crashpoint
        crashpoint("ff_after_intent")
        cas_ok = gitio.update_ref_cas(repo_path, repo["main_ref"], integration["candidate_commit"], integration["main_before"])
        crashpoint("ff_after_merge")
        result = _finish_ff(conn, repo_path, op_id, repo["main_ref"])
        return {"integration_id": integration_id, "approval_id": approval["approval_id"], "op_id": op_id,
                "state": "MERGED" if result["status"] == "APPLIED" else result["status"], "cas_ok": cas_ok,
                **restart_info}
