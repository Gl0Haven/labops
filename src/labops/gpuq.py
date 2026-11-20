"""Single-GPU queue orchestration for registered, repository-local entrypoints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sqlite3
import secrets
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from .canon import canonical_json
from .config import now
from .db import tx
from .errors import IllegalTransition, ValidationError
from .tasks import repo_row


_GPUQ_TRANSITIONS = {
    "DRAFT": {"QUEUED", "CANCELLED"},
    "QUEUED": {"ASKED", "LAUNCHED", "CANCELLED"},
    "ASKED": {"WAIT_MANUAL", "WAIT_AUTO", "LAUNCHING", "LAUNCHED", "CANCELLED"},
    "WAIT_MANUAL": {"LAUNCHING", "LAUNCHED", "CANCELLED"},
    "WAIT_AUTO": {"LAUNCHING", "LAUNCHED", "CANCELLED"},
    "LAUNCHING": {"LAUNCHED", "LAUNCH_FAILED", "CANCELLED"},
    "LAUNCHED": set(),
    "LAUNCH_FAILED": set(),
    "CANCELLED": set(),
}
_TERMINAL = {"LAUNCHED", "LAUNCH_FAILED", "CANCELLED"}
_ACTORS = {"codex", "opus", "secretary", "cli", "labops"}
_RUN_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_ALLOWED_ENTRY_ROOTS: tuple[Path, ...] = ()
_START_RUN_SCRIPT: Path | None = None


def _timestamp(value: str | None = None) -> str:
    return value or now()


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _normal_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(value.replace("/", "\\"))).rstrip("\\/").casefold()


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _long_path(path: Path) -> Path:
    """Expand Windows 8.3 short names (RUNNER~1) without following links.

    resolve() expands short names but also follows reparse points, which the entrypoint check must
    see first; GetLongPathNameW only rewrites the name components. Returns the input unchanged off
    Windows or when the path does not exist.
    """
    if os.name != "nt":
        return path
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetLongPathNameW(str(path), buffer, len(buffer))
    return Path(buffer.value) if 0 < length < len(buffer) else path


def _is_reparse(path: Path) -> bool:
    return path.is_symlink() or getattr(os.path, "isjunction", lambda _path: False)(str(path))


def _contains_reparse(path: Path, stop: Path) -> bool:
    current = path
    while True:
        if _is_reparse(current):
            return True
        if current == stop or current.parent == current:
            return False
        current = current.parent


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:80] or "gpu-run"


def _new_queue_id(timestamp: str) -> str:
    stamp = _parse_ts(timestamp).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"gq_{stamp}_{secrets.token_hex(4)}"


def _write_event(conn: sqlite3.Connection, *, queue_id: str, before: str | None,
                 state: str, actor: str, detail: dict | None, timestamp: str) -> None:
    conn.execute(
        "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) "
        "VALUES(?,?,?,?,?,?,?)",
        (timestamp, "gpu_queue", queue_id, before, state, actor,
         canonical_json(detail or {}).decode("utf-8")),
    )


def _transition_in_tx(conn: sqlite3.Connection, queue_id: str, to_state: str, *, actor: str,
                      detail: dict | None = None, timestamp: str | None = None,
                      updates: dict | None = None) -> dict:
    row = conn.execute("SELECT * FROM gpu_queue WHERE queue_id=?", (queue_id,)).fetchone()
    if row is None:
        raise ValidationError("queue_not_found", detail={"queue_id": queue_id})
    before = row["status"]
    if to_state not in _GPUQ_TRANSITIONS.get(before, set()):
        raise IllegalTransition("illegal_transition", detail={
            "entity_type": "gpu_queue", "entity_id": queue_id, "from": before, "to": to_state,
        })
    values = {"status": to_state, "updated_at": timestamp or now()}
    if updates:
        allowed = {"mode", "asked_at", "notified_idle_at", "launch_attempted_at",
                   "launched_run_key", "launch_error", "launch_fingerprint"}
        if set(updates) - allowed:
            raise ValidationError("invalid_queue_transition_update")
        values.update(updates)
    assignments = ",".join(f"{name}=?" for name in values)
    conn.execute(f"UPDATE gpu_queue SET {assignments} WHERE queue_id=?", (*values.values(), queue_id))
    _write_event(conn, queue_id=queue_id, before=before, state=to_state, actor=actor,
                 detail=detail, timestamp=timestamp or now())
    return dict(conn.execute("SELECT * FROM gpu_queue WHERE queue_id=?", (queue_id,)).fetchone())


def _transition(conn: sqlite3.Connection, queue_id: str, to_state: str, *, actor: str,
                 detail: dict | None = None, timestamp: str | None = None,
                 updates: dict | None = None) -> dict:
    with tx(conn):
        return _transition_in_tx(conn, queue_id, to_state, actor=actor, detail=detail,
                                 timestamp=timestamp, updates=updates)


def _claim_launch(conn: sqlite3.Connection, queue_id: str, *, actor: str, detail: dict,
                  updates: dict, timestamp: str, busy_snapshot: set[str] | frozenset[str]) -> dict:
    expected = set(busy_snapshot)
    try:
        with tx(conn):
            launching = conn.execute(
                "SELECT queue_id FROM gpu_queue WHERE status='LAUNCHING' ORDER BY created_at,queue_id LIMIT 1"
            ).fetchone()
            if launching is not None:
                raise ValidationError("queue_launch_in_progress", detail={"queue_id": launching["queue_id"]})
            current = {row[0] for row in conn.execute(
                "SELECT run_key FROM bg_runs WHERE status='running' AND orphaned=0"
            ).fetchall()}
            if current != expected:
                raise ValidationError("gpu_state_changed", detail={
                    "busy_snapshot": sorted(expected), "current_run_keys": sorted(current),
                })
            return _transition_in_tx(conn, queue_id, "LAUNCHING", actor=actor, detail=detail,
                                     timestamp=timestamp, updates=updates)
    except sqlite3.IntegrityError as exc:
        launching = conn.execute(
            "SELECT queue_id FROM gpu_queue WHERE status='LAUNCHING' ORDER BY created_at,queue_id LIMIT 1"
        ).fetchone()
        if launching is not None:
            raise ValidationError("queue_launch_in_progress", detail={"queue_id": launching["queue_id"]}) from exc
        raise


def _insert_draft(conn: sqlite3.Connection, *, repo_id: str, title: str, note: str,
                  created_by: str, timestamp: str) -> str:
    queue_id = _new_queue_id(timestamp)
    conn.execute(
        "INSERT INTO gpu_queue(queue_id,repo_id,title,note,created_by,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,'DRAFT',?,?)",
        (queue_id, repo_id, title, note, created_by, timestamp, timestamp),
    )
    _write_event(conn, queue_id=queue_id, before=None, state="DRAFT", actor=created_by,
                 detail={"note": note}, timestamp=timestamp)
    return queue_id


def queue_intent(conn: sqlite3.Connection, *, repo_id: str, title: str, note: str,
                 created_by: str = "secretary") -> dict:
    repo_row(conn, repo_id)
    if created_by not in {"secretary", "codex", "opus", "cli"}:
        raise ValidationError("invalid_queue_creator", detail={"created_by": created_by})
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
        raise ValidationError("invalid_queue_title")
    if not isinstance(note, str) or len(note) > 4000:
        raise ValidationError("invalid_queue_note")
    timestamp = now()
    with tx(conn):
        queue_id = _insert_draft(conn, repo_id=repo_id, title=title.strip(), note=note.strip(),
                                 created_by=created_by, timestamp=timestamp)
    return {"queue_item": dict(conn.execute("SELECT * FROM gpu_queue WHERE queue_id=?", (queue_id,)).fetchone())}


def _entry_roots(conn) -> tuple[Path, ...]:
    row = conn.execute("SELECT value FROM meta WHERE key='queue_entry_roots'").fetchone()
    return tuple(Path(path) for path in json.loads(row[0])) if row else tuple(_ALLOWED_ENTRY_ROOTS)


def _launcher_script(conn) -> Path | None:
    row = conn.execute("SELECT value FROM meta WHERE key='queue_launcher_script'").fetchone()
    return Path(row[0]) if row and row[0] else _START_RUN_SCRIPT


def queue_config(conn, *, entry_roots: list[str] | None = None, clear_entry_roots: bool = False,
                 launcher: str | None = None, clear_launcher: bool = False) -> dict:
    if entry_roots is not None:
        for value in entry_roots:
            path = Path(value)
            if value.startswith(("\\\\", "//")) or not path.is_absolute() or not path.is_dir():
                raise ValidationError("invalid_entry_root", detail={"path": value})
    if launcher is not None:
        path = Path(launcher)
        if not path.is_absolute() or path.suffix.casefold() != ".ps1" or not path.is_file():
            raise ValidationError("invalid_launcher", detail={"path": launcher})
    if entry_roots is not None or launcher is not None or clear_entry_roots or clear_launcher:
        with tx(conn):
            roots_json = None if entry_roots is None else canonical_json(list(dict.fromkeys(entry_roots))).decode("utf-8")
            for key, value, clear in (("queue_entry_roots", roots_json, clear_entry_roots),
                                      ("queue_launcher_script", launcher, clear_launcher)):
                if clear:
                    conn.execute("DELETE FROM meta WHERE key=?", (key,))
                elif value is not None:
                    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 (key, value))
    configured_launcher = _launcher_script(conn)
    return {"entry_roots": [str(root) for root in _entry_roots(conn)],
            "launcher": str(configured_launcher) if configured_launcher is not None else None}


def _validate_entrypoint(conn: sqlite3.Connection, repo_id: str, entrypoint: str) -> tuple[str, str]:
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise ValidationError("queue_entrypoint_required")
    candidate = Path(entrypoint).expanduser()
    if not candidate.is_absolute() or candidate.suffix.casefold() != ".ps1":
        raise ValidationError("queue_entrypoint_must_be_absolute_ps1", detail={"entrypoint": entrypoint})
    repo = repo_row(conn, repo_id)
    repo_path = Path(repo["path"]).resolve()
    raw_candidate = _long_path(Path(os.path.abspath(str(candidate))))
    if not _path_is_within(raw_candidate, repo_path):
        raise ValidationError("queue_entrypoint_outside_repo", detail={"entrypoint": entrypoint})
    if _contains_reparse(raw_candidate, repo_path):
        raise ValidationError("queue_entrypoint_reparse_point", detail={"entrypoint": entrypoint})
    try:
        resolved = raw_candidate.resolve(strict=True)
    except OSError as exc:
        raise ValidationError("queue_entrypoint_missing", detail={"entrypoint": entrypoint}) from exc
    if not resolved.is_file() or resolved.suffix.casefold() != ".ps1":
        raise ValidationError("queue_entrypoint_missing", detail={"entrypoint": entrypoint})
    if not _path_is_within(resolved, repo_path):
        raise ValidationError("queue_entrypoint_outside_repo", detail={"entrypoint": entrypoint})
    roots = _entry_roots(conn)
    if roots and not any(_path_is_within(resolved, root.resolve()) for root in roots):
        raise ValidationError("queue_entrypoint_outside_allowed_roots", detail={"entrypoint": entrypoint})
    if _contains_reparse(resolved, repo_path):
        raise ValidationError("queue_entrypoint_reparse_point", detail={"entrypoint": entrypoint})
    sha = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return str(resolved), sha


def _validate_config_paths(repo_path: str, config_paths: list[str] | None) -> list[str]:
    if config_paths is None:
        return []
    if not isinstance(config_paths, list) or not all(isinstance(value, str) for value in config_paths):
        raise ValidationError("invalid_queue_config_paths")
    if len(config_paths) > 10:
        raise ValidationError("too_many_queue_config_paths", detail={"maximum": 10})
    root = Path(repo_path).resolve()
    normalized = []
    for value in config_paths:
        pattern = value.strip().replace("\\", "/")
        windows_path = PureWindowsPath(value)
        posix_path = PurePosixPath(pattern)
        if (not pattern or "\x00" in pattern or windows_path.is_absolute() or windows_path.drive
                or posix_path.is_absolute() or ".." in posix_path.parts):
            raise ValidationError("invalid_queue_config_path", detail={"path": value})
        candidate = root.joinpath(*posix_path.parts)
        if _contains_reparse(candidate, root):
            raise ValidationError("queue_config_path_reparse_point", detail={"path": value})
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except FileNotFoundError as exc:
            raise ValidationError("queue_config_path_missing", detail={"path": value}) from exc
        except (OSError, ValueError) as exc:
            raise ValidationError("queue_config_path_outside_repo", detail={"path": value}) from exc
        if not resolved.is_file():
            raise ValidationError("queue_config_path_not_file", detail={"path": value})
        normalized_path = "/".join(posix_path.parts)
        if normalized_path not in normalized:
            normalized.append(normalized_path)
    return normalized


def _validate_run_prefix(title: str, run_prefix: str | None) -> str:
    result = _slug(title) if run_prefix is None else run_prefix
    if not isinstance(result, str) or not _RUN_PREFIX_RE.fullmatch(result):
        raise ValidationError("invalid_queue_run_prefix", detail={"run_prefix": result})
    return result


def _fingerprint(repo_path: str | Path, config_paths: list[str] | None) -> dict:
    """Best-effort, content-safe fingerprint for a toy or registered repository."""
    result = {
        "git_head": None,
        "git_dirty": None,
        "git_diff_sha256": None,
        "untracked_count": None,
        "configs": [],
        "captured_at": now(),
        "errors": {},
    }
    errors = result["errors"]
    try:
        root = Path(repo_path).resolve()
    except (OSError, RuntimeError, TypeError) as exc:
        root = Path(str(repo_path)).absolute()
        errors["repo_path"] = type(exc).__name__

    def git_output(args: list[str]) -> tuple[bytes | None, str | None]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), *args], capture_output=True, timeout=30,
                check=False, shell=False,
            )
        except subprocess.TimeoutExpired:
            return None, "timeout"
        except (OSError, ValueError) as exc:
            return None, type(exc).__name__
        if completed.returncode != 0:
            return None, f"git_exit_{completed.returncode}"
        return completed.stdout, None

    head, error = git_output(["rev-parse", "HEAD"])
    if error:
        errors["git_head"] = error
    elif head is not None:
        value = head.decode("utf-8", errors="replace").strip()
        if value:
            result["git_head"] = value
        else:
            errors["git_head"] = "empty_output"

    status, error = git_output(["status", "--porcelain", "-z"])
    if error:
        errors["git_dirty"] = error
    elif status is not None:
        result["git_dirty"] = bool(status)

    untracked, error = git_output(["ls-files", "--others", "--exclude-standard", "-z"])
    if error:
        errors["untracked_count"] = error
    elif untracked is not None:
        result["untracked_count"] = sum(1 for entry in untracked.split(b"\x00") if entry)

    diff, error = git_output(["diff", "HEAD", "--binary"])
    if error:
        errors["git_diff_sha256"] = error
    elif diff is not None:
        result["git_diff_sha256"] = hashlib.sha256(diff).hexdigest()

    for value in config_paths if isinstance(config_paths, list) else []:
        item = {"path": value, "sha256": None}
        try:
            if not isinstance(value, str):
                raise ValueError("invalid_path")
            relative = PurePosixPath(value.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts or PureWindowsPath(value).drive:
                raise ValueError("invalid_path")
            resolved = root.joinpath(*relative.parts).resolve(strict=True)
            resolved.relative_to(root)
            if not resolved.is_file():
                raise ValueError("not_a_file")
            item["sha256"] = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except FileNotFoundError:
            pass
        except (OSError, TypeError, ValueError) as exc:
            errors[f"config:{value}"] = str(exc) or type(exc).__name__
        result["configs"].append(item)
    return result


def _fingerprint_changes(registered: dict | None, launched: dict | None) -> list[str]:
    if not isinstance(registered, dict) or not isinstance(launched, dict):
        return []
    changed = [key for key in ("git_head", "git_dirty", "git_diff_sha256", "untracked_count")
               if registered.get(key) != launched.get(key)]
    before_configs = {item.get("path"): item.get("sha256") for item in registered.get("configs", [])
                      if isinstance(item, dict) and isinstance(item.get("path"), str)}
    after_configs = {item.get("path"): item.get("sha256") for item in launched.get("configs", [])
                     if isinstance(item, dict) and isinstance(item.get("path"), str)}
    changed.extend(path for path in sorted(set(before_configs) | set(after_configs))
                   if before_configs.get(path) != after_configs.get(path))
    return changed


def _fingerprint_summary(registered: dict | None, launched: dict | None,
                         changed: list[str]) -> str | None:
    if not changed or not isinstance(registered, dict) or not isinstance(launched, dict):
        return None
    parts = []
    if "git_head" in changed:
        before = str(registered.get("git_head") or "?")[:7]
        after = str(launched.get("git_head") or "?")[:7]
        parts.append(f"HEAD {before}→{after}")
    if "git_dirty" in changed:
        parts.append("工作区有改动" if launched.get("git_dirty") else "工作区已清洁")
    if "git_diff_sha256" in changed:
        parts.append("已跟踪文件 diff 已变")
    if "untracked_count" in changed:
        parts.append(f"未跟踪文件 {registered.get('untracked_count')}→{launched.get('untracked_count')}")
    before_configs = {item.get("path"): item.get("sha256") for item in registered.get("configs", [])
                      if isinstance(item, dict) and isinstance(item.get("path"), str)}
    after_configs = {item.get("path"): item.get("sha256") for item in launched.get("configs", [])
                     if isinstance(item, dict) and isinstance(item.get("path"), str)}
    config_paths = sorted(set(before_configs) | set(after_configs))
    parts.extend(f"{path} 已变" for path in config_paths
                 if before_configs.get(path) != after_configs.get(path))
    return "代码与登记时不同：" + "、".join(parts)


def _stored_fingerprint(value) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _queue_config_paths(queue: sqlite3.Row | dict) -> list[str]:
    raw = queue["config_paths"]
    if not raw:
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


def _ensure_launch_fingerprint(conn: sqlite3.Connection, queue: sqlite3.Row | dict,
                               timestamp: str) -> tuple[dict, str]:
    existing = _stored_fingerprint(queue["launch_fingerprint"])
    if existing is not None:
        return existing, canonical_json(existing).decode("utf-8")
    working_dir = queue["working_dir"]
    if not working_dir:
        working_dir = repo_row(conn, queue["repo_id"])["path"]
    value = _fingerprint(working_dir, _queue_config_paths(queue))
    encoded = canonical_json(value).decode("utf-8")
    with tx(conn):
        row = conn.execute("SELECT launch_fingerprint FROM gpu_queue WHERE queue_id=?",
                           (queue["queue_id"],)).fetchone()
        if row is not None and _stored_fingerprint(row["launch_fingerprint"]) is None:
            conn.execute("UPDATE gpu_queue SET launch_fingerprint=?,updated_at=? WHERE queue_id=?",
                         (encoded, timestamp, queue["queue_id"]))
            row = conn.execute("SELECT launch_fingerprint FROM gpu_queue WHERE queue_id=?",
                               (queue["queue_id"],)).fetchone()
    persisted = _stored_fingerprint(row["launch_fingerprint"] if row else None) or value
    return persisted, canonical_json(persisted).decode("utf-8")


def queue_register(conn: sqlite3.Connection, *, repo_id: str, title: str, entrypoint: str,
                   context: str, context_commit: str | None = None, draft_id: str | None = None,
                   run_prefix: str | None = None, created_by: str = "codex",
                   config_paths: list[str] | None = None) -> dict:
    if created_by not in {"codex", "opus", "cli"}:
        raise ValidationError("queue_register_actor_not_allowed", detail={"created_by": created_by})
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
        raise ValidationError("invalid_queue_title")
    if not isinstance(context, str) or not context.strip() or len(context.strip()) > 4000:
        raise ValidationError("invalid_queue_context")
    if context_commit is not None and not re.fullmatch(r"(?i)(?:[0-9a-f]{40}|[0-9a-f]{64})", context_commit):
        raise ValidationError("invalid_context_commit")
    resolved_entrypoint, entrypoint_sha = _validate_entrypoint(conn, repo_id, entrypoint)
    repo = repo_row(conn, repo_id)
    working_dir = str(Path(repo["path"]).resolve())
    normalized_configs = _validate_config_paths(repo["path"], config_paths)
    register_fingerprint = _fingerprint(working_dir, normalized_configs)
    config_json = canonical_json(normalized_configs).decode("utf-8")
    register_json = canonical_json(register_fingerprint).decode("utf-8")
    prefix = _validate_run_prefix(title.strip(), run_prefix)
    timestamp = now()
    details = {"entrypoint": resolved_entrypoint, "entrypoint_sha256": entrypoint_sha,
               "run_prefix": prefix, "config_paths": normalized_configs,
               "register_fingerprint": register_fingerprint}
    with tx(conn):
        if draft_id:
            existing = conn.execute("SELECT * FROM gpu_queue WHERE queue_id=?", (draft_id,)).fetchone()
            if existing is None:
                raise ValidationError("queue_not_found", detail={"queue_id": draft_id})
            if existing["status"] != "DRAFT":
                raise ValidationError("queue_draft_required", detail={"queue_id": draft_id, "status": existing["status"]})
            if existing["repo_id"] != repo_id:
                raise ValidationError("queue_repo_mismatch", detail={"queue_id": draft_id})
            conn.execute(
                "UPDATE gpu_queue SET title=?,entrypoint=?,entrypoint_sha256=?,working_dir=?,run_prefix=?,"
                "context=?,context_commit=?,config_paths=?,register_fingerprint=?,updated_at=? WHERE queue_id=?",
                (title.strip(), resolved_entrypoint, entrypoint_sha, working_dir, prefix, context.strip(),
                 context_commit, config_json, register_json, timestamp, draft_id),
            )
            queue_id = draft_id
            result = _transition_in_tx(conn, queue_id, "QUEUED", actor=created_by, detail=details,
                                       timestamp=timestamp)
        else:
            queue_id = _insert_draft(conn, repo_id=repo_id, title=title.strip(), note="",
                                     created_by=created_by, timestamp=timestamp)
            conn.execute(
                "UPDATE gpu_queue SET entrypoint=?,entrypoint_sha256=?,working_dir=?,run_prefix=?,"
                "context=?,context_commit=?,config_paths=?,register_fingerprint=?,updated_at=? WHERE queue_id=?",
                (resolved_entrypoint, entrypoint_sha, working_dir, prefix, context.strip(), context_commit,
                 config_json, register_json, timestamp, queue_id),
            )
            result = _transition_in_tx(conn, queue_id, "QUEUED", actor=created_by, detail=details,
                                       timestamp=timestamp)
    return {"queue_item": result}


def queue_cancel(conn: sqlite3.Connection, *, queue_id: str, reason: str, actor: str = "codex") -> dict:
    if actor not in {"codex", "opus", "cli"}:
        raise ValidationError("queue_cancel_actor_not_allowed", detail={"actor": actor})
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("queue_cancel_reason_required")
    return {"queue_item": _transition(conn, queue_id, "CANCELLED", actor=actor,
                                       detail={"reason": reason.strip()})}


def _pid_alive(pid: int | None, *, timeout: float = 10.0) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False, shell=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return True
    return re.search(rf"(?<!\d){pid}(?!\d)", result.stdout + "\n" + result.stderr) is not None


def _age_minutes(timestamp: str | None, now_ts: str) -> int | None:
    if not timestamp:
        return None
    try:
        return max(0, int((_parse_ts(now_ts) - _parse_ts(timestamp)).total_seconds() // 60))
    except (TypeError, ValueError):
        return None


def _record_orphaned(conn: sqlite3.Connection, row: sqlite3.Row, timestamp: str) -> None:
    with tx(conn):
        current = conn.execute("SELECT orphaned,status FROM bg_runs WHERE run_key=?", (row["run_key"],)).fetchone()
        if current is None or current["status"] != "running" or current["orphaned"]:
            return
        conn.execute("UPDATE bg_runs SET orphaned=1,updated_at=? WHERE run_key=?", (timestamp, row["run_key"]))
        conn.execute(
            "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) "
            "VALUES(?,'bg_run',?,'running','run_orphaned','labops',?)",
            (timestamp, row["run_key"], canonical_json({
                "repo_id": row["repo_id"], "run_dir": row["run_dir"], "pid": row["pid"],
                "progress_hint": row["progress_hint"],
            }).decode("utf-8")),
        )


def gpu_busy(conn: sqlite3.Connection, *, now_ts: str | None = None, pid_alive=None,
             process_info=None, run_key_snapshot: set[str] | None = None) -> dict:
    timestamp = now_ts or now()
    alive_check = pid_alive or _pid_alive
    rows = conn.execute(
        "SELECT run_key,repo_id,run_dir,status,pid,progress_hint,log_mtime,orphaned,source,"
        "process_started_at,title "
        "FROM bg_runs WHERE status='running' ORDER BY started_at,run_key"
    ).fetchall()
    active = []
    for row in rows:
        if run_key_snapshot is not None and not row["orphaned"]:
            run_key_snapshot.add(row["run_key"])
        if row["source"] == "adopted":
            from .runs import ProcessInfoUnavailable, _process_identity_matches, _process_info
            lookup = process_info or _process_info
            try:
                process = lookup(row["pid"])
                alive = _process_identity_matches(row["pid"], row["process_started_at"], process)
            except ProcessInfoUnavailable:
                # Treat an unavailable identity probe conservatively as busy.
                alive = True
        else:
            alive = bool(alive_check(row["pid"]))
        if not alive:
            if row["source"] != "adopted":
                _record_orphaned(conn, row, timestamp)
                if run_key_snapshot is not None:
                    run_key_snapshot.discard(row["run_key"])
            continue
        if row["orphaned"]:
            with tx(conn):
                conn.execute("UPDATE bg_runs SET orphaned=0,updated_at=? WHERE run_key=?",
                             (timestamp, row["run_key"]))
            if run_key_snapshot is not None:
                run_key_snapshot.add(row["run_key"])
        run_name = Path(str(row["run_dir"]).replace("\\", "/")).name or row["run_key"][:12]
        active.append({"repo_id": row["repo_id"], "run_dir_name": run_name,
                       "source": row["source"], "title": row["title"],
                       "progress_hint": row["progress_hint"],
                       "age_minutes": _age_minutes(row["log_mtime"], timestamp)})
    return {"busy": bool(active), "runs": active}


def queue_list(conn: sqlite3.Connection, *, pid_alive=None, now_ts: str | None = None) -> dict:
    rows = conn.execute(
        "SELECT * FROM gpu_queue WHERE status NOT IN ('LAUNCHED','LAUNCH_FAILED','CANCELLED') "
        "ORDER BY created_at,queue_id"
    ).fetchall()
    busy = gpu_busy(conn, now_ts=now_ts, pid_alive=pid_alive)
    return {"queue": [dict(row) for row in rows], "gpu": busy}


def queue_progress(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT queue_id,repo_id,title,status,mode,created_at,updated_at FROM gpu_queue "
        "WHERE status NOT IN ('LAUNCHED','LAUNCH_FAILED','CANCELLED') ORDER BY created_at,queue_id"
    ).fetchall()
    return {"count": len(rows), "items": [dict(row) for row in rows]}


def queue_choose(conn: sqlite3.Connection, *, queue_id: str, choice: str,
                 actor: str = "secretary", pid_alive=None, now_ts: str | None = None) -> dict:
    if actor not in {"secretary", "cli"}:
        raise ValidationError("queue_choose_actor_not_allowed", detail={"actor": actor})
    if choice not in {"manual", "auto", "now", "cancel"}:
        raise ValidationError("invalid_queue_choice", detail={"choice": choice})
    row = conn.execute("SELECT * FROM gpu_queue WHERE queue_id=?", (queue_id,)).fetchone()
    if row is None:
        raise ValidationError("queue_not_found", detail={"queue_id": queue_id})
    if row["status"] not in {"ASKED", "WAIT_MANUAL", "WAIT_AUTO"}:
        raise ValidationError("queue_choice_state_invalid", detail={"queue_id": queue_id, "status": row["status"]})
    timestamp = now_ts or now()
    if choice == "cancel":
        return {"queue_item": _transition(conn, queue_id, "CANCELLED", actor=actor,
                                           detail={"reason": "cancelled_by_user"}, timestamp=timestamp)}
    if choice in {"manual", "auto"}:
        if row["status"] != "ASKED":
            raise ValidationError("queue_choice_state_invalid", detail={"queue_id": queue_id, "status": row["status"]})
        target = "WAIT_MANUAL" if choice == "manual" else "WAIT_AUTO"
        return {"queue_item": _transition(conn, queue_id, target, actor=actor,
                                           detail={"choice": choice}, timestamp=timestamp,
                                           updates={"mode": choice})}
    if row["status"] not in {"ASKED", "WAIT_MANUAL"}:
        raise ValidationError("queue_choice_state_invalid", detail={"queue_id": queue_id, "status": row["status"]})
    busy_snapshot: set[str] = set()
    busy = gpu_busy(conn, now_ts=timestamp, pid_alive=pid_alive, run_key_snapshot=busy_snapshot)
    if busy["busy"]:
        raise ValidationError("gpu_busy", detail={"runs": busy["runs"]})
    return {"queue_item": _claim_launch(
        conn, queue_id, actor=actor, detail={"choice": "now"}, timestamp=timestamp,
        busy_snapshot=busy_snapshot,
        updates={"mode": "manual", "launch_error": None},
    )}


def _queue_launch_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key='queue_launch_mode'").fetchone()
    return row[0] if row and row[0] in {"handoff", "launcher"} else "handoff"


def _manual_command(queue: sqlite3.Row | dict, run_dir: str, launcher: Path | None) -> str:
    launcher_arg = str(launcher) if launcher is not None else "<launcher.ps1>"
    return subprocess.list2cmdline([
        shutil.which("pwsh") or "pwsh", "-NoProfile", "-File", launcher_arg,
        "-ScriptPath", queue["entrypoint"], "-WorkingDirectory", queue["working_dir"],
        "-RunDirectory", run_dir,
    ])


def _planned_run_dir(queue: sqlite3.Row | dict, timestamp: str) -> str:
    stamp = _parse_ts(timestamp).astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return str(Path(queue["working_dir"]) / "runs" / f"{queue['run_prefix']}_{stamp}")


def _previous_run(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT repo_id,run_dir,status,exit_code,finished_at FROM bg_runs "
        "WHERE status IN ('completed','failed','stopped','launch_incomplete') "
        "ORDER BY finished_at DESC,run_key DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {"run": "无前置 run", "result": ""}
    name = Path(str(row["run_dir"]).replace("\\", "/")).name
    if row["status"] == "completed":
        result = "完成"
    elif row["status"] in {"failed", "launch_incomplete"}:
        result = f"失败 exit {row['exit_code'] if row['exit_code'] is not None else '?'}"
    else:
        result = "已停止"
    return {"run": name, "result": result, "repo_id": row["repo_id"]}


def _emit_same_state_event(conn: sqlite3.Connection, queue: sqlite3.Row, *, actor: str,
                           detail: dict, timestamp: str) -> None:
    _write_event(conn, queue_id=queue["queue_id"], before=queue["status"],
                 state=queue["status"], actor=actor, detail=detail, timestamp=timestamp)


def _entry_hash_matches(queue: sqlite3.Row) -> bool:
    try:
        digest = hashlib.sha256(Path(queue["entrypoint"]).read_bytes()).hexdigest()
    except OSError:
        return False
    return digest == queue["entrypoint_sha256"]


def _invoke_launcher(queue: sqlite3.Row, run_dir: str, launcher: Path | None) -> dict:
    if launcher is None:
        return {"ok": False, "error": "launcher_not_configured"}
    pwsh = shutil.which("pwsh")
    if not pwsh:
        return {"ok": False, "error": "pwsh_not_found"}
    if not launcher.is_file():
        return {"ok": False, "error": "launcher_not_found"}
    if Path(run_dir).exists():
        return {"ok": False, "error": "run_directory_exists"}
    command = [pwsh, "-NoProfile", "-File", str(launcher),
               "-ScriptPath", queue["entrypoint"],
               "-WorkingDirectory", queue["working_dir"],
               "-RunDirectory", run_dir]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=120, check=False, shell=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "launcher_timeout"}
    except OSError as exc:
        return {"ok": False, "error": type(exc).__name__}
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").splitlines()[-1:] or [""]
        return {"ok": False, "error": f"launcher_exit_{result.returncode}: {tail[0][:160]}"}
    return {"ok": True}


def _match_runs(conn: sqlite3.Connection, timestamp: str) -> int:
    queues = conn.execute(
        "SELECT * FROM gpu_queue WHERE status NOT IN ('LAUNCHED','LAUNCH_FAILED','CANCELLED','DRAFT') "
        "AND entrypoint IS NOT NULL AND launched_run_key IS NULL ORDER BY created_at,queue_id"
    ).fetchall()
    runs = conn.execute(
        "SELECT run_key,repo_id,run_dir,script_path,status,exit_code,started_at,fingerprint_json FROM bg_runs "
        "WHERE script_path IS NOT NULL AND started_at IS NOT NULL ORDER BY started_at,run_key"
    ).fetchall()
    used = {row[0] for row in conn.execute(
        "SELECT launched_run_key FROM gpu_queue WHERE launched_run_key IS NOT NULL"
    )}
    matches = 0
    for queue in queues:
        queue_created = _parse_ts(queue["created_at"])
        for run in runs:
            if run["run_key"] in used or _normal_path(run["script_path"]) != _normal_path(queue["entrypoint"]):
                continue
            try:
                if _parse_ts(run["started_at"]) <= queue_created:
                    continue
            except (TypeError, ValueError):
                continue
            launched_fingerprint = _stored_fingerprint(queue["launch_fingerprint"])
            if launched_fingerprint is None:
                repo = repo_row(conn, queue["repo_id"])
                launched_fingerprint = _fingerprint(queue["working_dir"] or repo["path"],
                                                    _queue_config_paths(queue))
            fingerprint_json = canonical_json(launched_fingerprint).decode("utf-8")
            registered_fingerprint = _stored_fingerprint(queue["register_fingerprint"])
            changed = _fingerprint_changes(registered_fingerprint, launched_fingerprint)
            detail = {"notification_kind": "queue_launched", "run_key": run["run_key"],
                      "run_dir": run["run_dir"], "run_status": run["status"],
                      "exit_code": run["exit_code"], "fingerprint_changed": changed,
                      "fingerprint_summary": _fingerprint_summary(registered_fingerprint,
                                                                   launched_fingerprint, changed)}
            with tx(conn):
                _transition_in_tx(conn, queue["queue_id"], "LAUNCHED", actor="labops", detail=detail,
                                   timestamp=timestamp,
                                   updates={"launched_run_key": run["run_key"],
                                            "launch_fingerprint": fingerprint_json})
                conn.execute("UPDATE bg_runs SET fingerprint_json=? WHERE run_key=?",
                             (fingerprint_json, run["run_key"]))
            used.add(run["run_key"])
            matches += 1
            break
    return matches


def _store_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with tx(conn):
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))


def _queue_failed(conn: sqlite3.Connection, queue: sqlite3.Row, timestamp: str, reason: str,
                  run_dir: str | None = None) -> dict:
    command = _manual_command(queue, run_dir or _planned_run_dir(queue, timestamp), _launcher_script(conn))
    detail = {"notification_kind": "queue_launch_failed", "reason": reason,
              "manual_command": command, "entrypoint": queue["entrypoint"],
              "queue_id": queue["queue_id"]}
    return _transition(conn, queue["queue_id"], "LAUNCH_FAILED", actor="labops", detail=detail,
                       timestamp=timestamp, updates={"launch_attempted_at": timestamp,
                                                     "launch_error": reason})


def _handoff_launch(conn: sqlite3.Connection, queue: sqlite3.Row, timestamp: str) -> None:
    run_dir = _planned_run_dir(queue, timestamp)
    previous = _previous_run(conn)
    launch_fingerprint, fingerprint_json = _ensure_launch_fingerprint(conn, queue, timestamp)
    registered_fingerprint = _stored_fingerprint(queue["register_fingerprint"])
    changed = _fingerprint_changes(registered_fingerprint, launch_fingerprint)
    detail = {"notification_kind": "queue_idle_manual", "handoff": True,
              "previous_run": previous, "title": queue["title"], "repo_id": queue["repo_id"],
              "queue_id": queue["queue_id"], "entrypoint": queue["entrypoint"],
              "manual_command": _manual_command(queue, run_dir, _launcher_script(conn)),
              "fingerprint_changed": changed,
              "fingerprint_summary": _fingerprint_summary(registered_fingerprint,
                                                           launch_fingerprint, changed)}
    with tx(conn):
        _emit_same_state_event(conn, queue, actor="labops", detail=detail, timestamp=timestamp)
        conn.execute(
            "UPDATE gpu_queue SET launch_fingerprint=?,launch_attempted_at=COALESCE(launch_attempted_at,?),"
            "updated_at=? WHERE queue_id=?",
            (fingerprint_json, timestamp, timestamp, queue["queue_id"]),
        )


def queue_tick(conn: sqlite3.Connection, now_ts: str | None = None, *, pid_alive=None,
               launcher=None) -> dict:
    timestamp = now_ts or now()
    matched = _match_runs(conn, timestamp)
    busy_snapshot: set[str] = set()
    busy = gpu_busy(conn, now_ts=timestamp, pid_alive=pid_alive, run_key_snapshot=busy_snapshot)
    prior_busy_row = conn.execute("SELECT value FROM meta WHERE key='gpuq_last_busy'").fetchone()
    prior_busy = prior_busy_row[0] == "1" if prior_busy_row else None
    became_idle = prior_busy is True and not busy["busy"]
    _store_meta(conn, "gpuq_last_busy", "1" if busy["busy"] else "0")

    counts = {"matched": matched, "asked": 0, "manual_idle_notified": 0,
              "launching": 0, "launch_failed": 0, "handed_off": 0, "launch_contended": 0}
    rows = conn.execute(
        "SELECT * FROM gpu_queue WHERE status NOT IN ('LAUNCHED','LAUNCH_FAILED','CANCELLED') "
        "ORDER BY created_at,queue_id"
    ).fetchall()
    for queue in rows:
        if queue["status"] != "QUEUED":
            continue
        kind = "queue_ask_busy" if busy["busy"] else "queue_ask_idle"
        detail = {"notification_kind": kind, "title": queue["title"], "repo_id": queue["repo_id"],
                  "queue_id": queue["queue_id"], "gpu_runs": busy["runs"]}
        _transition(conn, queue["queue_id"], "ASKED", actor="labops", detail=detail,
                     timestamp=timestamp, updates={"asked_at": timestamp})
        counts["asked"] += 1
    if became_idle:
        waiting_manual = conn.execute(
            "SELECT * FROM gpu_queue WHERE status='WAIT_MANUAL' AND notified_idle_at IS NULL "
            "ORDER BY created_at,queue_id"
        ).fetchall()
        previous = _previous_run(conn)
        for queue in waiting_manual:
            command = _manual_command(queue, _planned_run_dir(queue, timestamp), _launcher_script(conn))
            launch_fingerprint, fingerprint_json = _ensure_launch_fingerprint(conn, queue, timestamp)
            registered_fingerprint = _stored_fingerprint(queue["register_fingerprint"])
            changed = _fingerprint_changes(registered_fingerprint, launch_fingerprint)
            detail = {"notification_kind": "queue_idle_manual", "previous_run": previous,
                      "title": queue["title"], "repo_id": queue["repo_id"],
                      "queue_id": queue["queue_id"], "entrypoint": queue["entrypoint"],
                      "manual_command": command, "fingerprint_changed": changed,
                      "fingerprint_summary": _fingerprint_summary(registered_fingerprint,
                                                                   launch_fingerprint, changed)}
            with tx(conn):
                _emit_same_state_event(conn, queue, actor="labops", detail=detail, timestamp=timestamp)
                conn.execute("UPDATE gpu_queue SET notified_idle_at=?,updated_at=?,"
                             "launch_fingerprint=? WHERE queue_id=? AND notified_idle_at IS NULL",
                             (timestamp, timestamp, fingerprint_json, queue["queue_id"]))
            counts["manual_idle_notified"] += 1

    launch_row = conn.execute(
        "SELECT * FROM gpu_queue WHERE status='LAUNCHING' ORDER BY created_at,queue_id LIMIT 1"
    ).fetchone()
    if launch_row is None and not busy["busy"]:
        auto_row = conn.execute(
            "SELECT * FROM gpu_queue WHERE status='WAIT_AUTO' ORDER BY created_at,queue_id LIMIT 1"
        ).fetchone()
        if auto_row is not None:
            launch_fingerprint = _fingerprint(auto_row["working_dir"], _queue_config_paths(auto_row))
            try:
                _claim_launch(
                    conn, auto_row["queue_id"], actor="labops", detail={"choice": "auto"},
                    timestamp=timestamp, busy_snapshot=busy_snapshot,
                    updates={"mode": "auto", "launch_error": None,
                             "launch_fingerprint": canonical_json(launch_fingerprint).decode("utf-8")},
                )
            except ValidationError as exc:
                if str(exc) not in {"queue_launch_in_progress", "gpu_state_changed"}:
                    raise
                counts["launch_contended"] += 1
            else:
                launch_row = conn.execute(
                    "SELECT * FROM gpu_queue WHERE queue_id=?", (auto_row["queue_id"],)
                ).fetchone()

    mode = _queue_launch_mode(conn)
    if launch_row is not None and not busy["busy"]:
        if _stored_fingerprint(launch_row["launch_fingerprint"]) is None:
            _ensure_launch_fingerprint(conn, launch_row, timestamp)
            launch_row = conn.execute("SELECT * FROM gpu_queue WHERE queue_id=?",
                                      (launch_row["queue_id"],)).fetchone()
        if mode == "handoff":
            if launch_row["launch_attempted_at"] is None:
                _handoff_launch(conn, launch_row, timestamp)
                counts["handed_off"] += 1
        elif launch_row["launch_attempted_at"] is not None:
            age = _age_minutes(launch_row["launch_attempted_at"], timestamp)
            if age is not None and age >= 10:
                failed = _queue_failed(conn, launch_row, timestamp, "run_not_observed")
                counts["launch_failed"] += 1
        elif not _entry_hash_matches(launch_row):
            _queue_failed(conn, launch_row, timestamp, "entrypoint_changed")
            counts["launch_failed"] += 1
        else:
            run_dir = _planned_run_dir(launch_row, timestamp)
            launch_result = (launcher(launch_row, run_dir) if launcher
                             else _invoke_launcher(launch_row, run_dir, _launcher_script(conn)))
            if not launch_result.get("ok"):
                _queue_failed(conn, launch_row, timestamp, launch_result.get("error", "launcher_failed"), run_dir)
                counts["launch_failed"] += 1
            else:
                with tx(conn):
                    conn.execute("UPDATE gpu_queue SET launch_attempted_at=?,updated_at=? WHERE queue_id=?",
                                 (timestamp, timestamp, launch_row["queue_id"]))
                counts["launching"] += 1
    return {"gpu_busy": busy, "launch_mode": mode, **counts}
