import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import desktop
from .errors import ValidationError
from .runs import ProcessInfoUnavailable, _process_identity_matches


def filetime_to_iso(ft: str) -> str:
    start = datetime(1601, 1, 1, tzinfo=timezone.utc)
    return (start + timedelta(microseconds=int(ft) // 10)).isoformat().replace("+00:00", "Z")


def list_sessions(claude_home: Path, *, process_info, limit: int) -> list[dict]:
    rows = []
    for path in sorted((claude_home / "sessions").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        pid, sid, pipe, start = (data.get(key) for key in ("pid", "sessionId", "messagingSocketPath", "procStart"))
        if (type(pid) is int and pid > 0 and isinstance(sid, str) and desktop.UUID_RE.fullmatch(sid)
                and isinstance(pipe, str) and isinstance(start, str) and re.fullmatch(r"[0-9]+", start)):
            rows.append(data)

    sessions = []
    for data in sorted(rows, key=lambda row: row["pid"])[:limit]:
        pid = data["pid"]
        try:
            expected_start = filetime_to_iso(data["procStart"])
        except (ValueError, OverflowError):
            continue
        try:
            alive = _process_identity_matches(pid, expected_start, process_info(pid))
        except ProcessInfoUnavailable:
            alive = None
        sessions.append({
            "session_id": data["sessionId"],
            "host_session_id": data.get("hostSessionId"),
            "name": data.get("name"),
            "cwd": data.get("cwd"),
            "entrypoint": data.get("entrypoint"),
            "status": data.get("status"),
            "pid": pid,
            "proc_start": data["procStart"],
            "alive": alive,
            "version": data.get("version"),
            "messaging_socket_path": data["messagingSocketPath"],
        })
    return sessions


def resolve_target(sessions: list[dict], target: str) -> dict:
    hits = [row for row in sessions if target in (row["session_id"], row["host_session_id"])]
    if not hits:
        hits = [row for row in sessions if row["name"] == target]
    if not hits:
        raise ValidationError("target_not_found", detail={"target": target})
    if len(hits) > 1:
        candidates = [{key: row[key] for key in ("session_id", "name", "cwd")} for row in hits]
        raise ValidationError("target_ambiguous", detail={"candidates": candidates})
    session = hits[0]
    if session["entrypoint"] != "claude-desktop":
        raise ValidationError("target_not_desktop", detail={"entrypoint": session["entrypoint"]})
    if session["alive"] is not True:
        raise ValidationError("target_not_running", detail={"pid": session["pid"], "alive": session["alive"]})
    return session


def canonical_pipe_key_name(pid: int, socket_path: str) -> str:
    match = re.fullmatch(r"[\\/]{2}[.?][\\/]pipe[\\/](?:(LOCAL)[\\/])?([^\\/]+)", socket_path, re.I)
    if match is None:
        raise ValidationError("key_unusable", detail={"reason": "socket_path"})
    name = match[2] if match[1] is None else "local\\" + match[2]
    canonical = ("\\\\.\\pipe\\" + name).lower()
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{pid}.{digest}.key"


def read_peer_token(claude_home: Path, session: dict) -> str:
    path = claude_home / "sessions" / canonical_pipe_key_name(session["pid"], session["messaging_socket_path"])
    try:
        with path.open("rb") as source:
            raw = source.read(4097)
        if len(raw) > 4096:
            raise ValueError
        key = json.loads(raw.decode("utf-8"))
        if not isinstance(key, dict):
            raise ValueError
    except (OSError, ValueError):
        raise ValidationError("key_unusable", detail={"reason": "read"}) from None
    token = key.get("peerToken")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValidationError("key_unusable", detail={"reason": "token"})
    if "procStartFt" in key and key["procStartFt"] != session["proc_start"]:
        raise ValidationError("key_unusable", detail={"reason": "stale"})
    return token


def build_lines(token: str, session_id: str, text: str, msg_id: str, uuid_: str) -> bytes:
    auth = {"type": "auth", "token": token}
    message = {"type": "user", "message": {"role": "user", "content": text},
               "from": "labops", "msg_id": msg_id, "uuid": uuid_, "session_id": session_id}
    return "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                   for row in (auth, message)).encode("utf-8")


def write_pipe(path: str, payload: bytes, *, opener) -> None:
    for attempt in range(desktop.PIPE_BUSY_RETRIES):
        try:
            stream = opener(path)
            break
        except OSError as exc:
            if getattr(exc, "winerror", None) == 231 and attempt < desktop.PIPE_BUSY_RETRIES - 1:
                time.sleep(0.2)
                continue
            raise desktop.SendFailedBeforeWrite("pipe_unavailable") from None

    try:
        written = stream.write(payload)
        if written != len(payload):
            raise OSError("short pipe write")
        stream.flush()
    except OSError:
        raise desktop.SendOutcomeUnknown("pipe_unavailable") from None
    finally:
        try:
            stream.close()
        except OSError:
            pass


def find_transcript(claude_home: Path, session_id: str) -> Path | None:
    matches = sorted((claude_home / "projects").glob(f"*/{session_id}.jsonl"), key=str)
    return matches[0] if matches else None


def _content(record: dict):
    message = record.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block["text"] for block in content if isinstance(block, dict)
                         and block.get("type") == "text" and isinstance(block.get("text"), str))
    return ""


def scan_transcript(path: Path, baseline_offset: int, marker: str) -> dict:
    seen = False
    delivered_at = None
    reply = None
    for record in desktop.read_complete_lines(path, baseline_offset):
        if record.get("isSidechain") is True:
            continue
        kind = record.get("type")
        content = _content(record)
        text = _text(content)
        if not seen:
            if kind == "user" and marker in text:
                seen = True
                delivered_at = record.get("timestamp")
            continue
        if kind == "user" and text.strip():
            break
        if kind == "assistant" and text.strip():
            reply = text
    return {"seen": seen, "delivered_at": delivered_at, "reply": reply}
