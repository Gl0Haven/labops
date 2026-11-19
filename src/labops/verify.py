import hashlib
import json
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import gitio
from .canon import canonical_json
from .config import ensure_layout, home, now
from .crash import crashpoint
from .db import tx
from .errors import Blocked, IllegalTransition, ValidationError
from .ids import new_id
from .locks import file_lock
from .statemachine import transition
from .tasks import repo_lock, repo_row, round_row, task_row

_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")
_ALLOWED_ENV = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "USERPROFILE",
                "HOMEDRIVE", "HOMEPATH", "COMSPEC", "PATHEXT", "LANG")
_LOCKFILE_NAMES = ("pyproject.toml", "uv.lock", "poetry.lock", "environment.yml")


def normalize_profile(profile: dict) -> dict:
    if not isinstance(profile, dict) or profile.get("schema") != 1:
        raise ValidationError("invalid_profile_schema")
    normalized = dict(profile)
    normalized.setdefault("commands", [])
    if isinstance(normalized["commands"], list):
        normalized["commands"] = [dict(command) if isinstance(command, dict) else command for command in normalized["commands"]]
    normalized.setdefault("requires_gpu", False)
    normalized.setdefault("allowed_paths", [])
    normalized.setdefault("max_log_bytes", 10 * 1024 * 1024)
    if not isinstance(normalized["commands"], list):
        raise ValidationError("invalid_profile_commands")
    for index, command in enumerate(normalized["commands"]):
        if not isinstance(command, dict):
            raise ValidationError("invalid_profile_command", detail={"index": index})
        name, argv = command.get("name"), command.get("argv")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ValidationError("invalid_command_name", detail={"index": index, "name": name})
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv):
            raise ValidationError("invalid_command_argv", detail={"index": index})
        for arg in argv:
            placeholders = _PLACEHOLDER_RE.findall(arg)
            residual = arg.replace("{python}", "")
            if any(value != "{python}" for value in placeholders) or "{" in residual or "}" in residual:
                raise ValidationError("invalid_command_placeholder", detail={"index": index, "arg": arg})
        cwd = command.get("cwd", ".")
        if not isinstance(cwd, str) or not cwd:
            raise ValidationError("invalid_command_cwd", detail={"index": index})
        timeout_s = command.get("timeout_s")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or not 1 <= timeout_s <= 7200:
            raise ValidationError("invalid_command_timeout", detail={"index": index, "timeout_s": timeout_s})
        env = command.get("env", {})
        if not isinstance(env, dict) or any(not isinstance(k, str) or not _ENV_RE.fullmatch(k) or not isinstance(v, str)
                                            for k, v in env.items()):
            raise ValidationError("invalid_command_env", detail={"index": index})
        command.setdefault("cwd", cwd)
        command.setdefault("env", env)
    if not isinstance(normalized["requires_gpu"], bool):
        raise ValidationError("invalid_requires_gpu")
    if not isinstance(normalized["allowed_paths"], list) or any(not isinstance(p, str) for p in normalized["allowed_paths"]):
        raise ValidationError("invalid_allowed_paths")
    max_log_bytes = normalized["max_log_bytes"]
    if isinstance(max_log_bytes, bool) or not isinstance(max_log_bytes, int) or max_log_bytes < 1:
        raise ValidationError("invalid_max_log_bytes")
    return normalized


def validate_profile(profile: dict) -> dict:
    return normalize_profile(profile)


def verify_lock_path(round_id: str) -> Path:
    return home() / "locks" / f"verify-{round_id.replace('/', '_')}.lock"


def _python_version(executable: str) -> str | None:
    if not Path(executable).is_file():
        return None
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, timeout=10,
                                env=_process_env({}), shell=False, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout + result.stderr).decode("utf-8", "replace").strip()
    return output or None


def _env_fingerprint(wt: Path, python_resolved: str | None = None, *, probe_python: bool = True) -> dict:
    git = shutil.which("git")
    git_version = None
    if git:
        result = subprocess.run([git, "--version"], capture_output=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        git_version = result.stdout.decode("utf-8", "surrogateescape").strip()
    candidates = [wt / name for name in _LOCKFILE_NAMES]
    candidates.extend(sorted(wt.glob("requirements*.txt")))
    locks = {}
    for path in candidates:
        if path.is_file():
            locks[path.relative_to(wt).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    executable = python_resolved or sys.executable
    return {"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
            "git": git_version, "lockfiles": locks, "python_resolved": executable,
            "python_version": _python_version(executable) if probe_python else None}


def _process_env(command_env: dict) -> dict[str, str]:
    env = {key: os.environ[key] for key in _ALLOWED_ENV if key in os.environ}
    env.update(command_env)
    return env


def _resolve_cwd(worktree: Path, cwd: str) -> Path:
    base = worktree.resolve()
    resolved = (base / cwd).resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValidationError("cwd_outside_worktree", detail={"cwd": cwd}) from exc
    return resolved


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10,
                           env=_process_env({}))
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _run_command(argv: list[str], *, cwd: Path, env: dict, timeout_s: int, max_log_bytes: int,
                 log_path: Path, python_executable: str | None = None) -> dict:
    actual_argv = [arg.replace("{python}", python_executable or sys.executable) for arg in argv]
    started = time.monotonic()
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(actual_argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                shell=False, creationflags=creationflags, start_new_session=(os.name != "nt"))
    except FileNotFoundError as exc:
        data = str(exc).encode("utf-8", "replace")
        _write_log(log_path, data, max_log_bytes)
        return {"exit_code": None, "duration_ms": int((time.monotonic() - started) * 1000), "timed_out": False,
                "blocked": True, "log_path": str(log_path), "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest()}
    captured = bytearray()
    overflow = threading.Event()

    def drain():
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            remaining = max_log_bytes - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow.set()

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    reader.join(timeout=10)
    if reader.is_alive() and proc.stdout:
        proc.stdout.close()
        reader.join(timeout=1)
    elif proc.stdout:
        proc.stdout.close()
    log_bytes = bytes(captured)
    if overflow.is_set():
        log_bytes += b"\n[labops: truncated]\n"
    _write_log(log_path, log_bytes, max_log_bytes + (len(b"\n[labops: truncated]\n") if overflow.is_set() else 0))
    return {"exit_code": proc.returncode, "duration_ms": int((time.monotonic() - started) * 1000),
            "timed_out": timed_out, "blocked": False, "log_path": str(log_path),
            "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest()}


def _write_log(path: Path, data: bytes, limit: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data[:limit])


def _evidence_insert(conn, *, evidence_id: str, round_id: str, commit: str, tree: str, profile_hash: str,
                     status: str, env_json: str | None = None, results_json: str | None = None,
                     producer: str = "labops") -> None:
    timestamp = now()
    conn.execute("INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,results_json,env_json,producer,created_at,finished_at) "
                 "VALUES(?,?,'INDEPENDENTLY_EXECUTED',?,?,?,?,?,?,?,?,?)",
                 (evidence_id, round_id, status, commit, tree, profile_hash, results_json, env_json, producer,
                  timestamp, timestamp if status != "running" else None))


def _finish_verify(conn, *, round_id: str, evidence_id: str, status: str, round_state: str,
                   results: list, env_info: dict, detail: dict | None = None) -> None:
    with tx(conn):
        r = round_row(conn, round_id)
        if r["state"] == "VERIFYING":
            transition(conn, "round", round_id, round_state, actor="labops", detail=detail or {"evidence_id": evidence_id})
        conn.execute("UPDATE evidence SET status=?,results_json=?,env_json=?,finished_at=? WHERE evidence_id=?",
                     (status, canonical_json(results).decode("utf-8"), canonical_json(env_info).decode("utf-8"), now(), evidence_id))


def _skip_result(command: dict) -> dict:
    return {"name": command["name"], "argv": command["argv"], "exit_code": None, "duration_ms": 0,
            "timed_out": False, "skipped": True, "log_path": None, "log_sha256": None}


def verify_round(conn: sqlite3.Connection, round_id: str, *, retry: bool = False) -> dict:
    r = round_row(conn, round_id)
    task = task_row(conn, r["task_id"])
    repo = repo_row(conn, task["repo_id"])
    repo_path = Path(repo["path"])
    profile_row = conn.execute("SELECT content_json FROM acceptance_profiles WHERE profile_hash=?", (task["profile_hash"],)).fetchone()
    if profile_row is None:
        raise ValidationError("profile_not_found", detail={"profile_hash": task["profile_hash"]})
    profile = normalize_profile(json.loads(profile_row[0]))
    allowed_states = {"CLAIMED"} | ({"BLOCKED"} if retry else set())
    if r["state"] not in allowed_states:
        raise IllegalTransition("round_not_verifiable", detail={"state": r["state"], "retry": retry})
    evidence_id = new_id("ev")
    verify_root = ensure_layout() / "verify" / evidence_id
    lock_path = verify_lock_path(round_id)
    python_executable = str(Path(repo["python"]).expanduser().resolve()) if repo["python"] else sys.executable
    python_missing = repo["python"] is not None and not Path(python_executable).is_file()
    if python_missing:
        reason = "repo_python_missing"
    else:
        reason = "repo_not_trusted_for_exec" if not repo["trusted_for_exec"] else "requires_external_run" if profile["requires_gpu"] else None
    # A profile with no commands executes nothing, so it must never yield INDEPENDENTLY_EXECUTED/passed
    # evidence (GPT review P0, 2026-10-21). Exemptions for doc-only tasks need an explicit decision.
    if reason is None and not profile["commands"]:
        reason = "no_checks"
    with file_lock(lock_path):
        with tx(conn):
            current = round_row(conn, round_id)
            if current["state"] not in allowed_states:
                raise IllegalTransition("round_not_verifiable", detail={"state": current["state"], "retry": retry})
            transition(conn, "round", round_id, "VERIFYING", actor="labops", detail={"evidence_id": evidence_id})
            _evidence_insert(conn, evidence_id=evidence_id, round_id=round_id, commit=current["head_commit"],
                             tree=current["head_tree"], profile_hash=task["profile_hash"], status="running")
        if reason:
            blocked_env = _env_fingerprint(repo_path, python_executable, probe_python=False)
            with tx(conn):
                transition(conn, "round", round_id, "BLOCKED", actor="labops", detail={"reason": reason})
                conn.execute("UPDATE evidence SET status='blocked',results_json='[]',env_json=?,finished_at=? WHERE evidence_id=?",
                             (canonical_json(blocked_env).decode("utf-8"), now(), evidence_id))
            raise Blocked(reason, detail={"round_id": round_id, "evidence_id": evidence_id})
        if verify_root.exists():
            with tx(conn):
                transition(conn, "round", round_id, "BLOCKED", actor="labops", detail={"reason": "verify_path_exists"})
                conn.execute("UPDATE evidence SET status='blocked',results_json='[]',finished_at=? WHERE evidence_id=?",
                             (now(), evidence_id))
            raise Blocked("verify_path_exists", detail={"path": str(verify_root)})
        verify_root.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(repo_lock(repo["repo_id"])):
            added = gitio.run(repo_path, "worktree", "add", "--detach", str(verify_root), r["head_commit"], read_only=False, check=False)
        if added.returncode:
            stderr = added.stderr.decode("utf-8", "surrogateescape")
            with tx(conn):
                transition(conn, "round", round_id, "BLOCKED", actor="labops", detail={"reason": "verify_worktree_failed"})
                conn.execute("UPDATE evidence SET status='blocked',results_json=?,finished_at=? WHERE evidence_id=?",
                             (canonical_json([{"error": stderr}]).decode(), now(), evidence_id))
            raise Blocked("verify_worktree_failed", detail={"stderr": stderr, "evidence_id": evidence_id})
        crashpoint("verify_after_start")
        env_info = _env_fingerprint(verify_root, python_executable)
        results = []
        failed = False
        blocked = False
        for command in profile["commands"]:
            argv = [arg.replace("{python}", python_executable) for arg in command["argv"]]
            try:
                cwd = _resolve_cwd(verify_root, command.get("cwd", "."))
            except ValidationError as exc:
                log_path = home() / "logs" / f"{evidence_id}-{command['name']}.log"
                result = {"exit_code": None, "duration_ms": 0, "timed_out": False, "blocked": True,
                          "log_path": str(log_path), "log_sha256": hashlib.sha256(b"").hexdigest()}
                _write_log(log_path, str(exc).encode(), profile["max_log_bytes"])
                result["log_sha256"] = hashlib.sha256(log_path.read_bytes()).hexdigest()
                is_blocked = result.pop("blocked")
                results.append({"name": command["name"], "argv": argv, **result})
                blocked = is_blocked
                break
            log_path = home() / "logs" / f"{evidence_id}-{command['name']}.log"
            result = _run_command(argv, cwd=cwd, env=_process_env(command.get("env", {})),
                                  timeout_s=command["timeout_s"], max_log_bytes=profile["max_log_bytes"], log_path=log_path,
                                  python_executable=python_executable)
            is_blocked = result.pop("blocked")
            results.append({"name": command["name"], "argv": argv, **result})
            if is_blocked:
                blocked = True
                break
            if result["timed_out"] or result["exit_code"] != 0:
                failed = True
                break
        if failed or blocked:
            for command in profile["commands"][len(results):]:
                results.append(_skip_result(command))
        crashpoint("verify_after_run")
        final_state = "BLOCKED" if blocked else "REJECTED" if failed else "VERIFIED"
        evidence_state = "blocked" if blocked else "failed" if failed else "passed"
        _finish_verify(conn, round_id=round_id, evidence_id=evidence_id, status=evidence_state,
                       round_state=final_state, results=results, env_info=env_info,
                       detail={"reason": "command_unavailable" if blocked else "command_failed" if failed else "verification_passed",
                               "evidence_id": evidence_id})
        with file_lock(repo_lock(repo["repo_id"])):
            removed = gitio.run(repo_path, "worktree", "remove", "--force", str(verify_root), read_only=False, check=False)
        if removed.returncode:
            with tx(conn):
                conn.execute("INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) VALUES(?,?,?,?,?,?,?)",
                             (now(), "evidence", evidence_id, None, None, "labops",
                              canonical_json({"cleanup_failed": removed.stderr.decode("utf-8", "surrogateescape")}).decode()))
        with file_lock(repo_lock(repo["repo_id"])):
            gitio.run(repo_path, "worktree", "prune", read_only=False, check=False)
    return {"round_id": round_id, "evidence_id": evidence_id, "state": final_state, "status": evidence_state,
            "results": results}
