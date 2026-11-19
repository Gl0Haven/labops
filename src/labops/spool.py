import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from .canon import canonical_json
from .config import home, now

_CLIENTS = {"codex", "claude"}
_EVENT_FIELDS = {"client", "event_type", "session_id", "turn_id", "cwd", "ts", "payload_hash"}
_MAX_NEW_EVENTS = 10000


def _compact_ts(value: str) -> str:
    return re.sub(r"\D", "", value)


def _event_id(ts: str, client: str, payload_hash: str) -> str:
    return f"{_compact_ts(ts)}-{client}-{payload_hash[:16]}"


def _string_field(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def write_event(client: str, raw: bytes) -> Path:
    if client not in _CLIENTS:
        raise ValueError("client must be codex or claude")
    timestamp = now()
    payload_hash = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        payload = None
    event_type = payload.get("hook_event_name") if isinstance(payload, dict) else None
    if not isinstance(event_type, str) or not event_type:
        event_type = "invalid_json"
    record = {
        "client": client,
        "event_type": event_type,
        "session_id": _string_field(payload, "session_id") if isinstance(payload, dict) else None,
        "turn_id": _string_field(payload, "turn_id") if isinstance(payload, dict) else None,
        "cwd": _string_field(payload, "cwd") if isinstance(payload, dict) else None,
        "ts": timestamp,
        "payload_hash": payload_hash,
    }
    event_id = _event_id(timestamp, client, payload_hash)
    spool_root = home() / "spool"
    tmp_dir = spool_root / "tmp"
    new_dir = spool_root / "new"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    new_dir.mkdir(parents=True, exist_ok=True)
    destination = new_dir / f"{event_id}.json"
    if sum(1 for _ in new_dir.glob("*.json")) >= _MAX_NEW_EVENTS and not destination.exists():
        overflow = spool_root / "overflow.log"
        with overflow.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{timestamp} dropped {event_id} because spool/new reached {_MAX_NEW_EVENTS} files\n")
        return destination
    temporary = tmp_dir / f"{event_id}.{os.getpid()}.tmp"
    try:
        with temporary.open("wb") as stream:
            stream.write(canonical_json(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def _valid_record(record, event_id: str) -> bool:
    if not isinstance(record, dict) or set(record) != _EVENT_FIELDS:
        return False
    if record["client"] not in _CLIENTS or not isinstance(record["event_type"], str) or not record["event_type"]:
        return False
    if not isinstance(record["ts"], str) or not isinstance(record["payload_hash"], str):
        return False
    if len(record["payload_hash"]) != 64 or any(ch not in "0123456789abcdef" for ch in record["payload_hash"]):
        return False
    if any(record[key] is not None and not isinstance(record[key], str) for key in ("session_id", "turn_id", "cwd")):
        return False
    return _event_id(record["ts"], record["client"], record["payload_hash"]) == event_id


def _move_file(source: Path, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / source.name
    if not source.exists():
        return
    if destination.exists():
        try:
            source.unlink()
        except FileNotFoundError:
            pass
    else:
        try:
            os.replace(source, destination)
        except FileNotFoundError:
            pass


def _repo_for_cwd(cwd: str, repos) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    common_dir = Path(result.stdout.decode("utf-8", "surrogateescape").strip())
    try:
        repo_path = common_dir.resolve().parent
    except OSError:
        return None
    key = os.path.normcase(os.path.normpath(str(repo_path)))
    for repo in repos:
        registered = os.path.normcase(os.path.normpath(str(Path(repo["path"]).resolve())))
        if registered == key:
            return repo["repo_id"]
    return None


def _drain_locked(conn) -> dict:
    spool_root = home() / "spool"
    new_dir = spool_root / "new"
    done_dir = spool_root / "done"
    bad_dir = spool_root / "bad"
    new_dir.mkdir(parents=True, exist_ok=True)
    done_dir.mkdir(parents=True, exist_ok=True)
    bad_dir.mkdir(parents=True, exist_ok=True)
    result = {"ingested": 0, "duplicates": 0, "invalid": 0, "repos_reconciled": 0}
    cwd_values = set()
    for path in sorted(new_dir.glob("*.json"), key=lambda item: item.name):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            event_id = path.stem
            if not _valid_record(record, event_id):
                raise ValueError("invalid spool event")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            result["invalid"] += 1
            _move_file(path, bad_dir)
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO spool_events(event_id,client,event_type,session_id,turn_id,cwd,ts,payload_hash,ingested_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, record["client"], record["event_type"], record["session_id"], record["turn_id"],
             record["cwd"], record["ts"], record["payload_hash"], now()),
        )
        if cursor.rowcount == 0:
            result["duplicates"] += 1
        else:
            result["ingested"] += 1
        if record["event_type"] == "invalid_json":
            result["invalid"] += 1
        if record["cwd"]:
            cwd_values.add(record["cwd"])
        _move_file(path, done_dir)

    repos = conn.execute("SELECT repo_id,path FROM repos ORDER BY repo_id").fetchall()
    repo_ids = {_repo_for_cwd(cwd, repos) for cwd in cwd_values}
    repo_ids.discard(None)
    if repo_ids:
        from .reconcile import reconcile
        for repo_id in sorted(repo_ids):
            reconcile(conn, repo_id)
    result["repos_reconciled"] = len(repo_ids)
    return result


def drain(conn) -> dict:
    from .locks import file_lock

    with file_lock(home() / "locks" / "spool-drain.lock", timeout_s=30):
        return _drain_locked(conn)
