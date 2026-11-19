import json
import os
import fnmatch
import hashlib
import re
import sqlite3
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import gitio
from .canon import canonical_json, sha256_hex
from .config import home, now
from .db import DEFAULT_HANDOFF_RULES, tx
from .errors import Blocked, GitConflict, IdempotencyConflict, UsageError, ValidationError, VersionConflict
from .ids import new_id, round_id as make_round_id, validate_actor, validate_repo_id, validate_role, validate_task_id
from .journal import OpSpec, apply_ref_op, get_operation, operation_by_idempotency, register_on_applied
from .locks import file_lock
from .statemachine import transition
from .runs import parse_progress_rules

_MAX_TASK_BRIEF_CHARS = 8000


def repo_lock(repo_id: str) -> Path:
    return home() / "locks" / f"repo-{repo_id}.lock"


def repo_row(conn: sqlite3.Connection, repo_id: str):
    row = conn.execute("SELECT * FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
    if row is None:
        raise ValidationError("repo_not_found", detail={"repo_id": repo_id})
    return row


def task_row(conn: sqlite3.Connection, task_id: str):
    row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise ValidationError("task_not_found", detail={"task_id": task_id})
    return row


def round_row(conn: sqlite3.Connection, round_id: str):
    row = conn.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
    if row is None:
        raise ValidationError("round_not_found", detail={"round_id": round_id})
    return row


def add_repo(conn: sqlite3.Connection, *, repo_id: str, path: Path, main_ref: str = "refs/heads/main",
             worktree_root: Path | None = None, trusted_for_exec: bool = False,
             confidential: bool = False) -> dict:
    validate_repo_id(repo_id)
    if not isinstance(confidential, bool):
        raise ValidationError("invalid_confidential_value", detail={"confidential": confidential})
    repo_path = Path(path).expanduser().resolve()
    if not repo_path.is_dir() or gitio.rev_parse(repo_path, "HEAD") is None:
        raise ValidationError("repo_not_git", detail={"path": str(repo_path)})
    if gitio.read_ref(repo_path, main_ref) is None:
        raise ValidationError("main_ref_not_found", detail={"main_ref": main_ref})
    resolved_worktree_root = str(Path(worktree_root).expanduser().resolve()) if worktree_root else None
    timestamp = now()
    with tx(conn):
        try:
            conn.execute("INSERT INTO repos(repo_id,path,main_ref,worktree_root,trusted_for_exec,confidential,created_at) VALUES(?,?,?,?,?,?,?)",
                         (repo_id, os.path.normcase(str(repo_path)), main_ref, resolved_worktree_root,
                          int(trusted_for_exec), int(confidential), timestamp))
        except sqlite3.IntegrityError as exc:
            raise ValidationError("repo_already_registered", detail={"repo_id": repo_id, "path": str(repo_path)}) from exc
        conn.execute("INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
                     (timestamp, "repo", repo_id, None, "REGISTERED", "user",
                      canonical_json({"path": str(repo_path), "trusted_for_exec": trusted_for_exec,
                                      "confidential": confidential}).decode()))
    return {"repo_id": repo_id, "path": str(repo_path), "main_ref": main_ref,
            "trusted_for_exec": trusted_for_exec, "confidential": confidential}


def list_repos(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT repo_id,path,main_ref,worktree_root,trusted_for_exec,confidential,python,backup_root,backup_keep,legacy_bridge "
        "FROM repos ORDER BY repo_id"
    ).fetchall()
    return [dict(row) for row in rows]


def set_repo_options(conn: sqlite3.Connection, repo_id: str, *, python: str | None = None,
                     backup_root: str | None = None, backup_keep: int | None = None,
                     legacy_bridge: str | None = None, run_watch: list[str] | None = None,
                     progress_rule: list[str] | None = None) -> dict:
    repo = repo_row(conn, repo_id)
    updates: dict[str, object] = {}
    if python is not None:
        if not python.strip():
            raise ValidationError("repo_python_path_required")
        candidate = Path(python).expanduser()
        if not candidate.is_absolute():
            raise ValidationError("repo_python_must_be_absolute", detail={"path": python})
        updates["python"] = str(candidate.resolve())
    if backup_root is not None:
        if not backup_root.strip():
            raise ValidationError("backup_root_path_required")
        candidate = Path(backup_root).expanduser()
        if not candidate.is_absolute():
            raise ValidationError("backup_root_must_be_absolute", detail={"path": backup_root})
        resolved = candidate.resolve()
        repo_path = Path(repo["path"]).resolve()
        try:
            resolved.relative_to(repo_path)
        except ValueError:
            pass
        else:
            raise ValidationError("backup_root_inside_repo", detail={"backup_root": str(resolved)})
        updates["backup_root"] = str(resolved)
    if backup_keep is not None:
        if isinstance(backup_keep, bool) or not isinstance(backup_keep, int) or backup_keep < 1:
            raise ValidationError("invalid_backup_keep", detail={"backup_keep": backup_keep})
        updates["backup_keep"] = backup_keep
    if legacy_bridge is not None:
        if not legacy_bridge.strip():
            raise ValidationError("legacy_bridge_path_required")
        relative = Path(legacy_bridge)
        windows_relative = PureWindowsPath(legacy_bridge)
        if relative.is_absolute() or windows_relative.is_absolute() or windows_relative.drive:
            raise ValidationError("legacy_bridge_must_be_relative", detail={"path": legacy_bridge})
        resolved = (Path(repo["path"]) / relative).resolve()
        try:
            resolved.relative_to(Path(repo["path"]).resolve())
        except ValueError as exc:
            raise ValidationError("legacy_bridge_outside_repo", detail={"path": legacy_bridge}) from exc
        updates["legacy_bridge"] = relative.as_posix()
    if run_watch is not None:
        values = [run_watch] if isinstance(run_watch, str) else list(run_watch)
        if not all(isinstance(value, str) for value in values):
            raise ValidationError("invalid_run_watch_globs")
        if values == [""] or not values:
            patterns = []
        else:
            patterns = []
            for value in values:
                pattern = value.strip().replace("\\", "/")
                windows_pattern = PureWindowsPath(value)
                posix_pattern = PurePosixPath(pattern)
                if (not pattern or windows_pattern.is_absolute() or windows_pattern.drive
                        or posix_pattern.is_absolute() or ".." in posix_pattern.parts or "\x00" in pattern):
                    raise ValidationError("invalid_run_watch_glob", detail={"pattern": value})
                if pattern not in patterns:
                    patterns.append(pattern)
        updates["run_watch_globs"] = patterns
    if progress_rule is not None:
        updates["run_progress_rules"] = parse_progress_rules(progress_rule)
    if not updates:
        raise ValidationError("repo_options_required")
    fields = ",".join(f"{name}=?" for name in updates)
    values = [canonical_json(value).decode("utf-8") if name in {"run_watch_globs", "run_progress_rules"} else value
              for name, value in updates.items()]
    with tx(conn):
        conn.execute(f"UPDATE repos SET {fields} WHERE repo_id=?", (*values, repo_id))
        conn.execute(
            "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
            (now(), "repo", repo_id, None, None, "user",
             canonical_json({"updated_fields": sorted(updates)}).decode()),
        )
    return {"repo_id": repo_id, **updates}


def get_handoff_rules(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key='handoff_rules'").fetchone()
    return row[0] if row is not None else DEFAULT_HANDOFF_RULES


def set_handoff_rules(conn: sqlite3.Connection, rules: str) -> dict:
    if not isinstance(rules, str) or not rules.strip():
        raise ValidationError("invalid_handoff_rules")
    current = get_handoff_rules(conn)
    if current == rules:
        return {"changed": False, "sha256": sha256_hex(rules.encode("utf-8"))}
    digest = sha256_hex(rules.encode("utf-8"))
    with tx(conn):
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('handoff_rules',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (rules,)
        )
        conn.execute(
            "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (now(), "meta", "handoff_rules", None, None, "user",
             canonical_json({"sha256": digest}).decode()),
        )
    return {"changed": True, "sha256": digest}


def set_task_brief(conn: sqlite3.Connection, task_id: str, brief: str) -> dict:
    if not isinstance(brief, str):
        raise ValidationError("invalid_task_brief")
    if len(brief) > _MAX_TASK_BRIEF_CHARS:
        raise ValidationError("task_brief_too_long", detail={"max_chars": _MAX_TASK_BRIEF_CHARS,
                                                               "actual_chars": len(brief)})
    with tx(conn):
        task = task_row(conn, task_id)
        if task["state"] not in {"CREATED", "ACTIVE"}:
            raise Blocked("task_brief_not_editable", detail={"task_id": task_id, "state": task["state"]})
        changed = task["brief"] != brief
        if changed:
            conn.execute("UPDATE tasks SET brief=?,updated_at=? WHERE task_id=?", (brief, now(), task_id))
    return {"task_id": task_id, "state": task["state"], "brief_chars": len(brief), "changed": changed}


def set_policy(conn: sqlite3.Connection, repo_id: str, policy: dict) -> dict:
    from .policy import validate_policy
    policy = validate_policy(policy)
    repo = repo_row(conn, repo_id)
    content = canonical_json(policy).decode("utf-8")
    digest = sha256_hex(content.encode("utf-8"))
    with tx(conn):
        conn.execute("UPDATE repos SET policy_json=?,policy_hash=? WHERE repo_id=?", (content, digest, repo_id))
        conn.execute("INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
                     (now(), "repo", repo_id, None, None, "user", canonical_json({"policy_hash": digest}).decode()))
    return {"repo_id": repo["repo_id"], "policy_hash": digest}


def set_confidential(conn: sqlite3.Connection, repo_id: str, confidential: bool) -> dict:
    if not isinstance(confidential, bool):
        raise ValidationError("invalid_confidential_value", detail={"confidential": confidential})
    repo = repo_row(conn, repo_id)
    value = int(confidential)
    if repo["confidential"] == value:
        return {"repo_id": repo_id, "confidential": confidential, "changed": False}
    with tx(conn):
        conn.execute("UPDATE repos SET confidential=? WHERE repo_id=?", (value, repo_id))
        conn.execute(
            "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
            (now(), "repo", repo_id, None, None, "user", canonical_json({"confidential": confidential}).decode()),
        )
    return {"repo_id": repo_id, "confidential": confidential, "changed": True}


def create_task(conn: sqlite3.Connection, *, repo_id: str, task_id: str, goal: str, profile: dict,
                base: str | None = None, brief: str | None = None) -> dict:
    validate_task_id(task_id)
    if not isinstance(goal, str) or not goal.strip():
        raise ValidationError("goal_required")
    if brief is not None and not isinstance(brief, str):
        raise ValidationError("invalid_task_brief")
    if brief is not None and len(brief) > _MAX_TASK_BRIEF_CHARS:
        raise ValidationError("task_brief_too_long", detail={"max_chars": _MAX_TASK_BRIEF_CHARS,
                                                               "actual_chars": len(brief)})
    repo = repo_row(conn, repo_id)
    repo_path = Path(repo["path"])
    base_commit = gitio.rev_parse(repo_path, base) if base else gitio.read_ref(repo_path, repo["main_ref"])
    if base_commit is None or gitio.object_type(repo_path, base_commit) != "commit":
        raise ValidationError("base_commit_not_found", detail={"base": base or repo["main_ref"]})
    from .verify import normalize_profile
    profile = normalize_profile(profile)
    profile_json = canonical_json(profile).decode("utf-8")
    profile_hash = sha256_hex(profile_json.encode("utf-8"))
    timestamp = now()
    with tx(conn):
        conn.execute("INSERT OR IGNORE INTO acceptance_profiles(profile_hash,content_json,created_at) VALUES(?,?,?)",
                     (profile_hash, profile_json, timestamp))
        try:
            conn.execute("INSERT INTO tasks(task_id,repo_id,goal,state,base_commit,profile_hash,policy_hash,policy_json,brief,source,created_at,updated_at) "
                         "VALUES(?,?,?,'CREATED',?,?,?,?,?,'native',?,?)",
                         (task_id, repo_id, goal, base_commit, profile_hash, repo["policy_hash"],
                          repo["policy_json"], brief, timestamp, timestamp))
        except sqlite3.IntegrityError as exc:
            raise ValidationError("task_already_exists", detail={"task_id": task_id}) from exc
        conn.execute("INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
                     (timestamp, "task", task_id, None, "CREATED", "user",
                      canonical_json({"repo_id": repo_id,
                                      "frozen_policy": json.loads(repo["policy_json"]) if repo["policy_json"] else None,
                                      "policy_hash": repo["policy_hash"]}).decode()))
    return {"task_id": task_id, "state": "CREATED", "base_commit": base_commit, "profile_hash": profile_hash,
            "policy_hash": repo["policy_hash"]}


def _record_round_open(conn: sqlite3.Connection, *, task_id: str, actor: str, role: str, base: str) -> dict:
    latest = conn.execute("SELECT MAX(n) FROM rounds WHERE task_id=?", (task_id,)).fetchone()[0]
    n = (latest or 0) + 1
    rid = make_round_id(task_id, n)
    timestamp = now()
    conn.execute("INSERT INTO rounds(round_id,task_id,n,actor,role,base_commit,state,created_at,updated_at) VALUES(?,?,?,?,?,?,'OPEN',?,?)",
                 (rid, task_id, n, actor, role, base, timestamp, timestamp))
    conn.execute("INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
                 (timestamp, "round", rid, None, "OPEN", actor, canonical_json({"base_commit": base}).decode()))
    return {"round_id": rid, "n": n, "actor": actor, "role": role, "state": "OPEN", "base_commit": base}


def open_round(conn: sqlite3.Connection, *, task_id: str, actor: str, role: str) -> dict:
    validate_actor(actor)
    validate_role(role)
    with tx(conn):
        task = task_row(conn, task_id)
        if conn.execute("SELECT 1 FROM rounds WHERE task_id=? AND state='OPEN' LIMIT 1", (task_id,)).fetchone():
            raise Blocked("round_open_exists", detail={"task_id": task_id})
        latest = conn.execute(
            "SELECT state FROM rounds WHERE task_id=? AND head_commit IS NOT NULL ORDER BY n DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if latest and latest["state"] in {"CLAIMED", "VERIFYING"}:
            raise Blocked("previous_round_not_settled", detail={"round_state": latest["state"]})
        latest_head = conn.execute("SELECT head_commit FROM rounds WHERE task_id=? AND head_commit IS NOT NULL ORDER BY n DESC LIMIT 1", (task_id,)).fetchone()
        base = latest_head[0] if latest_head else task["base_commit"]
        # The base is never moved silently (a direct commit on main would be absorbed into it);
        # instead report that main is ahead so the caller runs sync_main explicitly.
        main_ahead = False
        try:
            repo = repo_row(conn, task["repo_id"])
            repo_path = Path(repo["path"])
            main_tip = gitio.read_ref(repo_path, repo["main_ref"])
            main_ahead = bool(main_tip and main_tip != base and not gitio.is_ancestor(repo_path, main_tip, base))
        except (ValidationError, OSError):
            # Advisory only: an unreadable ref or unknown base must not block opening a round.
            main_ahead = False
        if task["state"] == "READY_FOR_INTEGRATION":
            transition(conn, "task", task_id, "ACTIVE", actor=actor, detail={"reason": "new_round"})
        elif task["state"] == "CREATED":
            transition(conn, "task", task_id, "ACTIVE", actor=actor, detail={"reason": "first_round"})
        elif task["state"] != "ACTIVE":
            raise Blocked("task_not_active", detail={"state": task["state"]})
        opened = _record_round_open(conn, task_id=task_id, actor=actor, role=role, base=base)
        if main_ahead:
            opened["main_ahead"] = True
            opened["hint"] = "main has advanced past this round's base: call sync_main(round_id) before starting work"
        return opened


def abandon_task(conn: sqlite3.Connection, task_id: str) -> dict:
    task = task_row(conn, task_id)
    with tx(conn):
        for rnd in conn.execute("SELECT round_id FROM rounds WHERE task_id=? AND state='OPEN' ORDER BY n", (task_id,)).fetchall():
            transition(conn, "round", rnd["round_id"], "ABANDONED", actor="user", detail={"reason": "task_abandoned"})
        transition(conn, "task", task_id, "ABANDONED", actor="user")
    return {"task_id": task_id, "state": "ABANDONED"}


def prune_worktrees(conn: sqlite3.Connection, task_id: str, *, apply: bool = False) -> dict:
    task = task_row(conn, task_id)
    if task["state"] not in {"INTEGRATED", "ABANDONED"}:
        raise Blocked("task_not_prunable", detail={"task_id": task_id, "state": task["state"]})
    repo = repo_row(conn, task["repo_id"])
    repo_path = Path(repo["path"]).resolve()
    root = Path(repo["worktree_root"]).resolve() if repo["worktree_root"] else (home() / "worktrees").resolve()
    registered = {
        os.path.normcase(str(Path(item["path"]).resolve())): item
        for item in gitio.worktrees(repo_path)
    }
    results = []
    removed_any = False
    rounds = conn.execute(
        "SELECT round_id,n,actor,branch,worktree_path FROM rounds WHERE task_id=? AND worktree_path IS NOT NULL ORDER BY n",
        (task_id,),
    ).fetchall()
    for rnd in rounds:
        path = Path(rnd["worktree_path"]).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            results.append({"round_id": rnd["round_id"], "path": str(path), "status": "skipped",
                            "reason": "outside_worktree_root"})
            continue
        key = os.path.normcase(str(path))
        expected_branch = f"refs/heads/labops/{task_id}/{rnd['actor']}-r{rnd['n']}"
        if (key not in registered or key == os.path.normcase(str(repo_path))
                or registered[key].get("branch") != expected_branch or rnd["branch"] != expected_branch.removeprefix("refs/heads/")):
            results.append({"round_id": rnd["round_id"], "path": str(path), "status": "skipped",
                            "reason": "not_registered_worktree"})
            continue
        if not path.is_dir():
            results.append({"round_id": rnd["round_id"], "path": str(path), "status": "skipped",
                            "reason": "worktree_missing"})
            continue
        dirty = gitio.status_porcelain(path)
        if dirty:
            results.append({"round_id": rnd["round_id"], "path": str(path), "status": "skipped",
                            "reason": "worktree_dirty", "dirty_files": [entry[1] for entry in dirty]})
            continue
        if not apply:
            results.append({"round_id": rnd["round_id"], "path": str(path), "status": "would_remove"})
            continue
        result = gitio.run(repo_path, "worktree", "remove", str(path), read_only=False, check=False)
        if result.returncode:
            results.append({"round_id": rnd["round_id"], "path": str(path), "status": "skipped",
                            "reason": "worktree_remove_failed",
                            "stderr": result.stderr.decode("utf-8", "surrogateescape").strip()})
            continue
        removed_any = True
        results.append({"round_id": rnd["round_id"], "path": str(path), "status": "removed"})
    if apply and removed_any:
        gitio.run(repo_path, "worktree", "prune", read_only=False)
    return {"task_id": task_id, "apply": apply, "worktrees": results}


def _round_claimed(conn: sqlite3.Connection, params: dict) -> None:
    rid = params["round_id"]
    row = round_row(conn, rid)
    target = (params["commit"], params["tree"], params["claim_id"])
    current = (row["head_commit"], row["head_tree"], row["claim_id"])
    if row["state"] == "OPEN":
        transition(conn, "round", rid, "CLAIMED", actor="labops", detail={"commit": target[0]})
    elif row["state"] == "CLAIMED" and current == target:
        return
    else:
        raise VersionConflict("round_claim_callback_conflict", detail={"round_id": rid, "state": row["state"], "bound": current})
    conn.execute("UPDATE rounds SET head_commit=?,head_tree=?,claim_id=?,updated_at=? WHERE round_id=?",
                 (*target, now(), rid))
    conn.execute("UPDATE claims SET status='accepted' WHERE claim_id=? AND status='received'", (params["claim_id"],))
    claim = conn.execute("SELECT payload_json,actor FROM claims WHERE claim_id=?", (params["claim_id"],)).fetchone()
    submitted_payload = json.loads(claim["payload_json"])
    self_tests = submitted_payload.get("notes", {}).get("self_tests")
    if self_tests is not None:
        status = _self_reported_status(self_tests)
        profile_hash = task_row(conn, row["task_id"])["profile_hash"]
        timestamp = now()
        conn.execute("INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,results_json,env_json,producer,created_at,finished_at) "
                     "VALUES(?,?,'SELF_REPORTED',?,?,?,?,?,NULL,?,?,?)",
                     (new_id("ev"), rid, status, params["commit"], params["tree"], profile_hash,
                      canonical_json(self_tests).decode("utf-8"), claim["actor"], timestamp, timestamp))
    task_id, n = row["task_id"], row["n"]
    old_reviews = conn.execute("SELECT review_id,state FROM reviews WHERE task_id=? AND round_id IN (SELECT round_id FROM rounds WHERE task_id=? AND n<?) AND state!='STALE'",
                               (task_id, task_id, n)).fetchall()
    for review in old_reviews:
        transition(conn, "review", review["review_id"], "STALE", actor="labops", detail={"reason": f"superseded_by:{rid}"})


def _worktree_recorded(conn: sqlite3.Connection, params: dict) -> None:
    row = round_row(conn, params["round_id"])
    if row["branch"] == params["branch"] and row["worktree_path"] == params["path"]:
        return
    if row["branch"] is not None or row["worktree_path"] is not None:
        raise ValidationError("worktree_record_conflict", detail={"round_id": params["round_id"]})
    conn.execute("UPDATE rounds SET branch=?,worktree_path=?,updated_at=? WHERE round_id=?",
                 (params["branch"], params["path"], now(), params["round_id"]))


register_on_applied("round_claimed", _round_claimed)
register_on_applied("worktree_recorded", _worktree_recorded)


def _self_reported_status(value) -> str:
    if isinstance(value, dict):
        passed = value.get("passed") is True or value.get("status") == "passed"
    elif isinstance(value, list):
        passed = bool(value) and all(isinstance(item, dict) and (item.get("passed") is True or item.get("status") == "passed") for item in value)
    else:
        passed = value is True
    return "passed" if passed else "failed"


def _record_rejected_claim(conn: sqlite3.Connection, *, rid: str, actor: str, request_id: str,
                           payload: dict, payload_hash: str, reason: str, fingerprint: str | None) -> None:
    conn.execute("INSERT INTO claims(claim_id,round_id,actor,kind,client_request_id,payload_json,payload_hash,fingerprint,status,reject_reason,created_at) "
                 "VALUES(?,?,?,'submit_changes',?,?,?,?,'rejected',?,?)",
                 (new_id("cl"), rid, actor, request_id, canonical_json(payload).decode(), payload_hash,
                  fingerprint, reason, now()))


def submit_changes(conn: sqlite3.Connection, *, round_id: str, actor: str, commit: str,
                   client_request_id: str, notes: dict | None = None, source: str = "cli") -> dict:
    if source not in {"cli", "mcp"}:
        raise ValidationError("invalid_claim_source", detail={"source": source})
    if notes is not None and not isinstance(notes, dict):
        raise ValidationError("notes_must_be_object")
    payload = {"commit": commit, "notes": notes or {}}
    payload_hash = sha256_hex(canonical_json(payload))
    r0 = round_row(conn, round_id)
    task0 = task_row(conn, r0["task_id"])
    repo0 = repo_row(conn, task0["repo_id"])
    repo_path = Path(repo0["path"])
    with file_lock(repo_lock(repo0["repo_id"])):
        with tx(conn):
            r = round_row(conn, round_id)
            if actor != r["actor"]:
                raise ValidationError("actor_mismatch", detail={"actor": actor, "expected": r["actor"]})
            existing = conn.execute("SELECT * FROM claims WHERE round_id=? AND actor=? AND kind='submit_changes' AND client_request_id=?",
                                    (round_id, actor, client_request_id)).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise IdempotencyConflict("request_id_payload_mismatch", detail={"claim_id": existing["claim_id"]})
                return {"claim_id": existing["claim_id"], "status": existing["status"], "replayed": True}
        full = gitio.rev_parse(repo_path, f"{commit}^{{commit}}")
        if full is None:
            with tx(conn):
                _record_rejected_claim(conn, rid=round_id, actor=actor, request_id=client_request_id, payload=payload,
                                       payload_hash=payload_hash, reason="commit_not_found", fingerprint=None)
            raise ValidationError("commit_not_found", detail={"commit": commit})
        r = round_row(conn, round_id)
        task = task_row(conn, r["task_id"])
        fingerprint = f"{task['task_id']}|{r['n']}|{actor}|{full}"
        rejection = None
        with tx(conn):
            dup = conn.execute("SELECT * FROM claims WHERE fingerprint=? AND status IN ('accepted','received') ORDER BY created_at LIMIT 1",
                               (fingerprint,)).fetchone()
            if dup:
                claim_id = new_id("cl")
                conn.execute("INSERT INTO claims(claim_id,round_id,actor,kind,client_request_id,payload_json,payload_hash,fingerprint,status,duplicate_of,created_at,source) "
                             "VALUES(?,?,?,'submit_changes',?,?,?,?, 'duplicate',?,?,?)",
                             (claim_id, round_id, actor, client_request_id, canonical_json(payload).decode(), payload_hash,
                              fingerprint, dup["claim_id"], now(), source))
                return {"claim_id": claim_id, "status": "duplicate", "duplicate_of": dup["claim_id"]}
            if r["state"] != "OPEN":
                raise VersionConflict("round_already_bound", detail={"round_state": r["state"], "bound_head": r["head_commit"]})
            # Opus review fix: a 'received' claim for another SHA means a submit crashed between
            # intent and CAS; reconcile must settle it before any other SHA can bind this round.
            pending = conn.execute("SELECT claim_id FROM claims WHERE round_id=? AND kind='submit_changes' AND status='received' LIMIT 1",
                                   (round_id,)).fetchone()
            if pending:
                raise VersionConflict("round_claim_pending", detail={"pending_claim_id": pending["claim_id"]})
            if full == r["base_commit"]:
                _record_rejected_claim(conn, rid=round_id, actor=actor, request_id=client_request_id, payload=payload,
                                       payload_hash=payload_hash, reason="empty_round", fingerprint=fingerprint)
                rejection = "empty_round"
            elif not gitio.is_ancestor(repo_path, r["base_commit"], full):
                _record_rejected_claim(conn, rid=round_id, actor=actor, request_id=client_request_id, payload=payload,
                                       payload_hash=payload_hash, reason="not_descendant_of_base", fingerprint=fingerprint)
                rejection = "not_descendant_of_base"
            else:
                claim_id = new_id("cl")
                conn.execute("INSERT INTO claims(claim_id,round_id,actor,kind,client_request_id,payload_json,payload_hash,fingerprint,status,created_at,source) "
                             "VALUES(?,?,?,'submit_changes',?,?,?,?, 'received',?,?)",
                             (claim_id, round_id, actor, client_request_id, canonical_json(payload).decode(), payload_hash,
                              fingerprint, now(), source))
        if rejection == "empty_round":
            raise ValidationError("empty_round", detail={"commit": full})
        if rejection == "not_descendant_of_base":
            raise ValidationError("not_descendant_of_base", detail={"base": r["base_commit"], "commit": full})
        spec = OpSpec(repo0["repo_id"], "create_ref", f"refs/labops/{task['task_id']}/r{r['n']}/{actor}/submitted",
                      None, full, f"submit:{claim_id}",
                      {"type": "round_claimed", "round_id": round_id, "commit": full,
                       "tree": gitio.tree_of(repo_path, full), "claim_id": claim_id})
        op = apply_ref_op(conn, repo_path, spec)
        if op["status"] == "CONFLICT":
            with tx(conn):
                conn.execute("UPDATE claims SET status='rejected',reject_reason='ref_conflict' WHERE claim_id=?", (claim_id,))
            raise GitConflict("submitted_ref_conflict", detail={"ref": spec.ref, "actual": gitio.read_ref(repo_path, spec.ref)})
        return {"claim_id": claim_id, "status": "accepted" if op["status"] == "APPLIED" else op["status"].lower(), "op_id": op["op_id"]}


def create_worktree(conn: sqlite3.Connection, round_id: str) -> dict:
    r = round_row(conn, round_id)
    task = task_row(conn, r["task_id"])
    repo = repo_row(conn, task["repo_id"])
    repo_path = Path(repo["path"])
    root = Path(repo["worktree_root"]) if repo["worktree_root"] else home() / "worktrees"
    branch = f"labops/{task['task_id']}/{r['actor']}-r{r['n']}"
    wt_path = root / repo["repo_id"] / f"{task['task_id']}-{r['actor']}-r{r['n']}"
    wt_ref = f"refs/heads/{branch}"
    idem = f"wt:{round_id}"
    with file_lock(repo_lock(repo["repo_id"])):
        old = operation_by_idempotency(conn, idem)
        if old:
            if old["status"] == "APPLIED":
                return {"round_id": round_id, "branch": branch, "path": str(wt_path), "op_id": old["op_id"], "status": "APPLIED"}
            return {"round_id": round_id, "branch": branch, "path": str(wt_path), "op_id": old["op_id"], "status": old["status"]}
        with tx(conn):
            op_id = new_id("op")
            timestamp = now()
            conn.execute("INSERT INTO operations(op_id,repo_id,kind,ref,expected_old,target,status,idempotency_id,on_applied_json,created_at,updated_at) "
                         "VALUES(?,?, 'create_worktree', ?,NULL,?,'PENDING',?,?,?,?)",
                         (op_id, repo["repo_id"], wt_ref, r["base_commit"], idem,
                          canonical_json({"type": "worktree_recorded", "round_id": round_id,
                                          "branch": branch, "path": str(wt_path)}).decode(), timestamp, timestamp))
        from .crash import crashpoint
        if wt_path.exists():
            _finish_worktree_op(conn, repo_path, op_id, branch, wt_path, failed={"reason": "path_exists"})
            return {"round_id": round_id, "branch": branch, "path": str(wt_path), "op_id": op_id, "status": "FAILED"}
        if gitio.read_ref(repo_path, wt_ref) is not None:
            _finish_worktree_op(conn, repo_path, op_id, branch, wt_path, conflict={"reason": "branch_exists"})
            return {"round_id": round_id, "branch": branch, "path": str(wt_path), "op_id": op_id, "status": "CONFLICT"}
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        result = gitio.run(repo_path, "worktree", "add", "-b", branch, str(wt_path), r["base_commit"], read_only=False, check=False)
        if result.returncode:
            stderr = result.stderr.decode("utf-8", "surrogateescape")
            status = "CONFLICT" if gitio.read_ref(repo_path, wt_ref) is not None else "FAILED"
            _finish_worktree_op(conn, repo_path, op_id, branch, wt_path,
                                conflict={"stderr": stderr} if status == "CONFLICT" else None,
                                failed={"stderr": stderr} if status == "FAILED" else None)
            return {"round_id": round_id, "branch": branch, "path": str(wt_path), "op_id": op_id, "status": status}
        crashpoint("wt_after_add")
        _finish_worktree_op(conn, repo_path, op_id, branch, wt_path)
        row = get_operation(conn, op_id)
        return {"round_id": round_id, "branch": branch, "path": str(wt_path), "op_id": op_id, "status": row["status"]}


def _finish_worktree_op(conn: sqlite3.Connection, repo_path: Path, op_id: str, branch: str, wt_path: Path,
                        *, failed: dict | None = None, conflict: dict | None = None) -> None:
    current = get_operation(conn, op_id)
    if current["status"] != "PENDING":
        return
    records = gitio.worktrees(repo_path)
    target_path = os.path.normcase(str(wt_path.resolve()))
    match = next((w for w in records if os.path.normcase(str(Path(w["path"]).resolve())) == target_path), None)
    if match and match["branch"] == f"refs/heads/{branch}":
        with tx(conn):
            transition(conn, "operation", op_id, "APPLIED", actor="labops")
            row = get_operation(conn, op_id)
            from .journal import _apply_callback
            _apply_callback(conn, row["on_applied_json"])
        return
    if failed is None and conflict is None:
        if wt_path.exists():
            failed = {"reason": "path_exists"}
        elif gitio.read_ref(repo_path, f"refs/heads/{branch}") is not None:
            conflict = {"reason": "branch_exists"}
        else:
            failed = {"reason": "worktree_not_created"}
    status, detail = ("CONFLICT", conflict) if conflict is not None else ("FAILED", failed)
    with tx(conn):
        conn.execute("UPDATE operations SET detail_json=?,updated_at=? WHERE op_id=?",
                     (canonical_json(detail).decode(), now(), op_id))
        transition(conn, "operation", op_id, status, actor="labops", detail=detail)


_DENY_GLOBS = ("*.pt", "*.pth", "*.ckpt", "*.safetensors", "*.bin", "*.onnx", "*.npz", "*.pkl",
               ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "data/**", "runs/**", "checkpoints/**")
_SECRET_PATTERNS = {
    "openai_key": re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(rb"ghp_[A-Za-z0-9]{36}"),
    "github_pat": re.compile(rb"github_pat_[A-Za-z0-9_]{40,}"),
    "aws_access_key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "private_key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "slack_token": re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
}


def _normalize_snapshot_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise UsageError("invalid_snapshot_path", detail={"path": value})
    normalized = value.replace("\\", "/")
    win_path = PureWindowsPath(value)
    posix_path = PurePosixPath(normalized)
    if posix_path.is_absolute() or win_path.is_absolute() or win_path.drive or ".." in posix_path.parts:
        raise UsageError("snapshot_path_must_be_relative", detail={"path": value})
    if normalized in ("", "."):
        raise UsageError("invalid_snapshot_path", detail={"path": value})
    return posix_path.as_posix()


def _guard_snapshot(wt: Path, files: set[str], deleted_files: set[str] | None = None) -> None:
    deleted_files = deleted_files or set()
    blocked = []
    for rel in sorted(files):
        if any(fnmatch.fnmatchcase(rel, pattern) for pattern in _DENY_GLOBS):
            blocked.append({"path": rel, "reason": "deny_glob"})
            continue
        if rel in deleted_files:
            continue
        target = wt.joinpath(*rel.split("/"))
        parent = target.parent
        while parent != wt and parent != parent.parent:
            if parent.is_symlink() or getattr(os.path, "isjunction", lambda _p: False)(str(parent)):
                blocked.append({"path": rel, "reason": "path_reparse_point"})
                break
            parent = parent.parent
        if blocked and blocked[-1].get("path") == rel and blocked[-1].get("reason") == "path_reparse_point":
            continue
        try:
            info = target.lstat()
        except FileNotFoundError:
            raise ValidationError("snapshot_file_missing", detail={"path": rel})
        if info.st_size > 5 * 1024 * 1024:
            blocked.append({"path": rel, "reason": "file_too_large", "size": info.st_size})
            continue
        if target.is_symlink():
            content = os.readlink(target).encode("utf-8", "surrogateescape")
        elif target.is_file():
            content = target.read_bytes()
        else:
            raise ValidationError("snapshot_path_not_file", detail={"path": rel})
        for rule, pattern in _SECRET_PATTERNS.items():
            if pattern.search(content):
                blocked.append({"path": rel, "reason": "secret_pattern", "rule": rule})
                break
    if blocked:
        raise Blocked("snapshot_guard_blocked", detail={"matches": blocked})


def _finish_snapshot(conn: sqlite3.Connection, op_id: str, status: str, *, target: str | None = None,
                     detail: dict | None = None) -> None:
    with tx(conn):
        row = get_operation(conn, op_id)
        if row["status"] != "PENDING":
            return
        if target is not None:
            conn.execute("UPDATE operations SET target=?,updated_at=? WHERE op_id=?", (target, now(), op_id))
        if detail is not None:
            conn.execute("UPDATE operations SET detail_json=?,updated_at=? WHERE op_id=?",
                         (canonical_json(detail).decode("utf-8"), now(), op_id))
        transition(conn, "operation", op_id, status, actor="labops", detail=detail)


def snapshot(conn: sqlite3.Connection, *, round_id: str, files: list[str], message: str, actor: str) -> dict:
    r = round_row(conn, round_id)
    task = task_row(conn, r["task_id"])
    repo = repo_row(conn, task["repo_id"])
    repo_path = Path(repo["path"])
    if actor != r["actor"]:
        raise ValidationError("actor_mismatch", detail={"actor": actor, "expected": r["actor"]})
    normalized = {_normalize_snapshot_path(f) for f in files}
    if not normalized:
        raise UsageError("snapshot_files_required")
    if not isinstance(message, str) or not message.strip():
        raise UsageError("snapshot_message_required")
    digest_input = canonical_json({"files": sorted(normalized), "message": message})
    digest = hashlib.sha256(digest_input).hexdigest()[:16]
    if not r["worktree_path"] or not r["branch"]:
        raise Blocked("round_worktree_missing", detail={"round_id": round_id})
    wt = Path(r["worktree_path"])
    with file_lock(repo_lock(repo["repo_id"])):
        return _snapshot_locked(conn, r, task, repo, repo_path, wt, normalized, digest, message, actor)


def _snapshot_locked(conn: sqlite3.Connection, r, task, repo, repo_path: Path, wt: Path,
                     normalized: set[str], digest: str, message: str, actor: str) -> dict:
    # Opus review fix: the idempotency key includes the HEAD the snapshot starts from. Keying on
    # files+message alone made a second "wip" snapshot of the same file silently replay the first
    # commit and leave the new edits uncommitted. A replay is recognised only when the earlier
    # snapshot's commit is still the worktree HEAD.
    current_head = gitio.rev_parse(wt, "HEAD")
    replay = conn.execute("SELECT * FROM operations WHERE kind='snapshot_commit' AND status='APPLIED' AND target=? "
                          "AND idempotency_id GLOB ? ORDER BY created_at DESC LIMIT 1",
                          (current_head, f"snap:{r['round_id']}:{digest}:*")).fetchone() if current_head else None
    if replay is not None and not ({p.replace("\\", "/") for _, p in gitio.status_porcelain(wt)} & normalized):
        return {"commit": replay["target"], "op_id": replay["op_id"], "replayed": True}
    idem = f"snap:{r['round_id']}:{digest}:{current_head}"
    existing = operation_by_idempotency(conn, idem)
    if existing:
        if existing["status"] == "APPLIED":
            return {"commit": existing["target"], "op_id": existing["op_id"], "replayed": True}
        raise Blocked("snapshot_operation_not_applied", detail={"op_id": existing["op_id"], "status": existing["status"]})
    target_path = os.path.normcase(str(wt.resolve()))
    registered = next((w for w in gitio.worktrees(repo_path)
                       if os.path.normcase(str(Path(w["path"]).resolve())) == target_path
                       and w["branch"] == f"refs/heads/{r['branch']}"), None)
    if registered is None:
        raise Blocked("round_worktree_mismatch", detail={"round_id": r["round_id"], "path": str(wt), "branch": r["branch"]})
    status_entries = gitio.status_porcelain(wt)
    dirty = {path.replace("\\", "/") for _, path in status_entries}
    deleted_files = {path.replace("\\", "/") for xy, path in status_entries if "D" in xy}
    missing = normalized - dirty
    if missing:
        raise ValidationError("not_modified", detail={"paths": sorted(missing)})
    unlisted = dirty - normalized
    if unlisted:
        raise Blocked("unlisted_dirty", detail={"paths": sorted(unlisted)})
    _guard_snapshot(wt, normalized, deleted_files)
    head_before = gitio.rev_parse(wt, "HEAD")
    if head_before is None:
        raise ValidationError("worktree_head_not_found")
    with tx(conn):
        op_id = new_id("op")
        timestamp = now()
        conn.execute("INSERT INTO operations(op_id,repo_id,kind,ref,expected_old,target,status,idempotency_id,on_applied_json,created_at,updated_at) "
                     "VALUES(?,?,'snapshot_commit',NULL,?,NULL,'PENDING',?,NULL,?,?)",
                     (op_id, repo["repo_id"], head_before, idem, timestamp, timestamp))
    for rel in sorted(normalized):
        result = gitio.run(wt, "add", "--", f":(literal){rel}", read_only=False, check=False)
        if result.returncode:
            stderr = result.stderr.decode("utf-8", "surrogateescape")
            if "index.lock" in stderr:
                _finish_snapshot(conn, op_id, "FAILED", detail={"reason": "index_lock_present"})
                raise Blocked("index_lock_present", detail={"reason": "index_lock_present", "path": str(wt / ".git" / "index.lock")})
            _finish_snapshot(conn, op_id, "FAILED", detail={"reason": "git_add_failed", "stderr": stderr})
            raise ValidationError("git_add_failed", detail={"path": rel, "stderr": stderr})
    from .crash import crashpoint
    crashpoint("snap_after_add")
    trailers = ["--trailer", f"LabOps-Task: {task['task_id']}", "--trailer", f"LabOps-Round: {r['n']}",
                "--trailer", f"LabOps-Actor: {actor}"]
    commit_result = gitio.run(wt, "commit", "-m", message, *trailers, read_only=False, check=False)
    if commit_result.returncode:
        stderr = commit_result.stderr.decode("utf-8", "surrogateescape")
        if "index.lock" in stderr:
            _finish_snapshot(conn, op_id, "FAILED", detail={"reason": "index_lock_present"})
            raise Blocked("index_lock_present", detail={"reason": "index_lock_present", "path": str(wt / ".git" / "index.lock")})
        _finish_snapshot(conn, op_id, "FAILED", detail={"reason": "git_commit_failed", "stderr": stderr})
        raise GitConflict("git_commit_failed", detail={"stderr": stderr})
    crashpoint("snap_after_commit")
    new_head = gitio.rev_parse(wt, "HEAD")
    parent = gitio.rev_parse(wt, f"{new_head}^") if new_head else None
    actual_trailers = gitio.commit_trailers(wt, new_head) if new_head else {}
    expected_trailers = {"LabOps-Task": task["task_id"], "LabOps-Round": str(r["n"]), "LabOps-Actor": actor}
    if new_head is None or parent != head_before or any(actual_trailers.get(k) != v for k, v in expected_trailers.items()):
        _finish_snapshot(conn, op_id, "FAILED", target=new_head, detail={"reason": "snapshot_commit_mismatch", "parent": parent,
                                                                          "trailers": actual_trailers})
        raise GitConflict("snapshot_commit_mismatch", detail={"commit": new_head})
    _finish_snapshot(conn, op_id, "APPLIED", target=new_head)
    return {"commit": new_head, "op_id": op_id, "replayed": False}


def sync_main(conn: sqlite3.Connection, *, round_id: str, actor: str) -> dict:
    """Merge the current main into an OPEN round's worktree so its head stays fast-forwardable.

    The merge commit is recorded as a snapshot_commit operation keyed snap:<round>:sync:..., so
    reconcile attributes it like any other LabOps snapshot (REL-5).
    """
    r = round_row(conn, round_id)
    task = task_row(conn, r["task_id"])
    repo = repo_row(conn, task["repo_id"])
    repo_path = Path(repo["path"])
    if actor != r["actor"]:
        raise ValidationError("actor_mismatch", detail={"actor": actor, "expected": r["actor"]})
    if r["state"] != "OPEN":
        raise Blocked("round_not_open", detail={"round_id": round_id, "state": r["state"]})
    if not r["worktree_path"] or not r["branch"]:
        raise Blocked("round_worktree_missing", detail={"round_id": round_id})
    wt = Path(r["worktree_path"])
    with file_lock(repo_lock(repo["repo_id"])):
        main_tip = gitio.read_ref(repo_path, repo["main_ref"])
        if main_tip is None:
            raise ValidationError("main_ref_not_found", detail={"main_ref": repo["main_ref"]})
        head_before = gitio.rev_parse(wt, "HEAD")
        if head_before is None:
            raise ValidationError("worktree_head_not_found")
        if gitio.is_ancestor(repo_path, main_tip, head_before):
            return {"commit": head_before, "main": main_tip, "merged": False, "reason": "already_contains_main"}
        if gitio.status_porcelain(wt):
            raise Blocked("worktree_dirty", detail={"round_id": round_id})
        idem = f"snap:{round_id}:sync:{main_tip}:{head_before}"
        existing = operation_by_idempotency(conn, idem)
        if existing is not None and existing["status"] == "APPLIED":
            return {"commit": existing["target"], "main": main_tip, "merged": True, "op_id": existing["op_id"], "replayed": True}
        with tx(conn):
            op_id = new_id("op")
            timestamp = now()
            conn.execute("INSERT INTO operations(op_id,repo_id,kind,ref,expected_old,target,status,idempotency_id,on_applied_json,created_at,updated_at) "
                         "VALUES(?,?,'snapshot_commit',NULL,?,NULL,'PENDING',?,NULL,?,?)",
                         (op_id, repo["repo_id"], head_before, idem, timestamp, timestamp))
        message = (f"Merge main {main_tip[:12]} into {task['task_id']} r{r['n']}\n\n"
                   f"LabOps-Task: {task['task_id']}\nLabOps-Round: {r['n']}\nLabOps-Actor: {actor}")
        merged = gitio.run(wt, "merge", "--no-ff", "-m", message, main_tip, read_only=False, check=False)
        if merged.returncode:
            gitio.run(wt, "merge", "--abort", read_only=False, check=False)
            stderr = merged.stderr.decode("utf-8", "surrogateescape") + merged.stdout.decode("utf-8", "surrogateescape")
            _finish_snapshot(conn, op_id, "FAILED", detail={"reason": "merge_conflict", "output": stderr[-2000:]})
            raise GitConflict("sync_main_conflict", detail={"main": main_tip, "head": head_before})
        new_head = gitio.rev_parse(wt, "HEAD")
        parents = gitio.run(wt, "rev-list", "--parents", "-n", "1", new_head, check=False).stdout.decode().split() if new_head else []
        if parents[1:] != [head_before, main_tip]:
            _finish_snapshot(conn, op_id, "FAILED", target=new_head, detail={"reason": "sync_commit_mismatch", "parents": parents[1:]})
            raise GitConflict("sync_commit_mismatch", detail={"commit": new_head})
        _finish_snapshot(conn, op_id, "APPLIED", target=new_head)
        return {"commit": new_head, "main": main_tip, "merged": True, "op_id": op_id, "replayed": False}
