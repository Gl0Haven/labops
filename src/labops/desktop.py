import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from . import desktop_claude, desktop_codex, runs, tasks
from .canon import canonical_json
from .config import now
from .db import tx
from .errors import IdempotencyConflict, ValidationError

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
MAX_PROMPT_CHARS = 20000
ORIGINS = ("mcp", "wechat", "codex")
WECHAT_PREFIX = "[来自微信转发 · 经 LabOps secretary · 非本人在场授权；需要确认的操作请先用 notify_user 回报]"
WECHAT_MAX_PROMPT_CHARS = 4000
WECHAT_DISPATCH_PER_HOUR = 20
CODEX_PREFIX = "[来自 Codex · 经 LabOps · 非用户本人授权；涉及裁定的请先报用户确认]"
CODEX_MESSAGE_PER_HOUR = 20
CODEX_IPC_TIMEOUT_S = 20
CODEX_SEND_BUDGET_S = 40
PIPE_BUSY_RETRIES = 3
STATUS_READ_MAX_BYTES = 16 * 1024 * 1024


class SendFailedBeforeWrite(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SendOutcomeUnknown(SendFailedBeforeWrite):
    pass


def _claude_home() -> Path:
    return Path(os.environ.get("LABOPS_CLAUDE_HOME") or os.environ.get("CLAUDE_HOME") or Path.home() / ".claude")


def _codex_home() -> Path:
    return Path(os.environ.get("LABOPS_CODEX_HOME") or os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _check_repo(conn, repo_id: str, cwd: str | None) -> None:
    repo_path = tasks.repo_row(conn, repo_id)["path"]
    root = os.path.normcase(os.path.realpath(repo_path))
    target = os.path.normcase(os.path.realpath(cwd)) if cwd is not None else None
    if target is None or (target != root and not target.startswith(root.rstrip(os.sep) + os.sep)):
        raise ValidationError("target_repo_mismatch", detail={"cwd": cwd, "repo_path": repo_path})


def read_complete_lines(path: Path, offset: int):
    with path.open("rb") as source:
        source.seek(offset)
        data = source.read(STATUS_READ_MAX_BYTES)
    for line in data.split(b"\n")[:-1]:
        try:
            row = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def prepare_claude(conn, target, repo_id, marker, process_info):
    home = _claude_home()
    sessions = desktop_claude.list_sessions(home, process_info=process_info, limit=50)
    session = desktop_claude.resolve_target(sessions, target)
    if repo_id:
        _check_repo(conn, repo_id, session["cwd"])
    token = desktop_claude.read_peer_token(home, session)
    transcript = desktop_claude.find_transcript(home, session["session_id"])
    detail = {key: session[key] for key in ("session_id", "host_session_id", "pid", "proc_start", "cwd", "name")}
    detail.update(claude_version=session["version"], transcript_path=str(transcript) if transcript else None,
                  baseline_offset=transcript.stat().st_size if transcript else 0, marker=marker, msg_id=str(uuid4()))
    return session["messaging_socket_path"], token, detail


def _open_claude_pipe(path):
    return open(path, "wb", buffering=0)


def send_claude(path, token, detail, prompt, opener) -> dict:
    payload = desktop_claude.build_lines(token, detail["session_id"], detail["marker"] + "\n" + prompt,
                                         detail["msg_id"], str(uuid4()))
    desktop_claude.write_pipe(path, payload, opener=opener)
    return {}


def _codex_timeout(deadline: float) -> float:
    return max(0.0, min(CODEX_IPC_TIMEOUT_S, deadline - time.monotonic()))


def _close_codex(client) -> None:
    try:
        client.stream.close()
    except OSError:
        pass


def prepare_codex(conn, target, repo_id, marker, codex_connect):
    thread_id = desktop_codex.normalize_thread_target(target)
    if not UUID_RE.fullmatch(thread_id):
        raise ValidationError("codex_thread_unknown", detail={"reason": "format"})
    rollout = desktop_codex.find_rollout(_codex_home(), thread_id)
    if rollout is None:
        raise ValidationError("codex_thread_unknown", detail={"reason": "no_rollout"})
    meta = desktop_codex.read_session_meta(rollout)
    if repo_id:
        _check_repo(conn, repo_id, meta.get("cwd"))

    deadline = time.monotonic() + CODEX_SEND_BUDGET_S
    pipe = os.environ.get("LABOPS_CODEX_IPC_PIPE") or r"\\.\pipe\codex-ipc"
    client = codex_connect(pipe)
    try:
        try:
            init = client.request("initialize", {"clientType": "labops"}, version=1,
                                  timeout=_codex_timeout(deadline))
        except (desktop_codex.IpcError, desktop_codex.IpcTimeout):
            raise ValidationError("codex_initialize_failed") from None
        client_id = init.get("clientId") if isinstance(init, dict) else None
        if not isinstance(client_id, str) or not client_id:
            raise ValidationError("codex_initialize_failed")
        client.client_id = client_id
        try:
            owner_result = client.request("thread-owner-discovery", {"hostId": "local", "conversationId": thread_id},
                                          version=1, timeout=_codex_timeout(deadline))
        except (desktop_codex.IpcError, desktop_codex.IpcTimeout) as exc:
            raise ValidationError("codex_thread_not_open", detail={"reason": str(exc)[:200]}) from None
        # Live Codex 26.1111.2228: the owner is on the response envelope, result is
        # {"supportsUntrustedAppInput": true}.
        owner = client.last_response.get("handledByClientId")
        if not isinstance(owner, str) or not owner:
            raise ValidationError("codex_thread_not_open")
        detail = {"thread_id": thread_id, "owner_client_id": owner, "turn_id": None,
                  "client_user_message_id": str(uuid4()), "rollout_path": str(rollout),
                  "baseline_offset": rollout.stat().st_size, "marker": marker,
                  "cli_version": meta.get("cli_version"), "cwd": meta.get("cwd")}
    except BaseException:
        _close_codex(client)
        raise
    client._send_deadline = deadline
    return client, detail


def send_codex(client, detail, prompt) -> dict:
    deadline = client._send_deadline
    if deadline - time.monotonic() < 1:
        raise SendFailedBeforeWrite("codex_start_turn_failed")
    thread_id = detail["thread_id"]
    text = detail["marker"] + "\n" + prompt
    params = {
        "conversationId": thread_id,
        "turnStart": {
            "request": {"threadId": thread_id, "input": [{"type": "text", "text": text, "text_elements": []}],
                        "attachments": [], "clientUserMessageId": detail["client_user_message_id"],
                        "additionalContext": {}},
            "context": {"inheritThreadSettings": True},
        },
        "isSteering": False,
    }
    try:
        result = client.request("thread-follower-start-turn", params, version=2, target=detail["owner_client_id"],
                                timeout=_codex_timeout(deadline))
    except desktop_codex.IpcTimeout:
        raise SendOutcomeUnknown("codex_start_turn_failed") from None
    except desktop_codex.IpcError as exc:
        # Live 2026-11-19: "thread-follower-start-turn-timeout" came back while the turn had in fact
        # started, so a timeout-style error means the outcome is unknown, not "not delivered" (DESK-3).
        if str(exc).endswith("-timeout"):
            raise SendOutcomeUnknown("codex_start_turn_failed") from None
        raise SendFailedBeforeWrite("codex_start_turn_failed") from None
    return {"turn_id": desktop_codex.extract_turn_id(result)}


def _request_row(conn, request_id):
    return conn.execute("SELECT * FROM desktop_requests WHERE request_id=?", (request_id,)).fetchone()


def _row_view(row) -> dict:
    view = {key: row[key] for key in ("request_id", "kind", "target", "repo_id", "state", "error",
                                     "created_at", "updated_at")}
    return view | {"detail": json.loads(row["detail_json"])}


def _replay(row, kind, target, prompt_sha, origin) -> dict:
    detail = json.loads(row["detail_json"])
    matches = (target in (row["target"], detail.get("host_session_id"), detail.get("name")) if kind == "claude"
               else desktop_codex.normalize_thread_target(target) == row["target"])
    if row["kind"] == kind and row["prompt_sha256"] == prompt_sha and row["origin"] == origin and matches:
        return _row_view(row) | {"replayed": True}
    raise IdempotencyConflict("idempotency_conflict", detail={"request_id": row["request_id"]})


def desktop_send(conn, *, kind: str, target: str, prompt: str, request_id: str,
                 actor: str, repo_id: str | None = None, origin: str = "mcp", process_info=None,
                 claude_open=None, codex_connect=None) -> dict:
    if origin not in ORIGINS:
        raise ValidationError("invalid_origin")
    if kind not in ("claude", "codex"):
        raise ValidationError("invalid_kind")
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise ValidationError("invalid_request_id")
    if not prompt.strip():
        raise ValidationError("prompt_empty")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValidationError("prompt_too_long", detail={"chars": len(prompt), "max": MAX_PROMPT_CHARS})
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    old = _request_row(conn, request_id)
    if old is not None:
        return _replay(old, kind, target, prompt_sha, origin)

    marker = f"[labops:{request_id}]"
    prefix = {"wechat": WECHAT_PREFIX, "codex": CODEX_PREFIX}.get(origin)
    wire_prompt = prefix + "\n" + prompt if prefix else prompt
    client = None
    if kind == "claude":
        path, token, detail = prepare_claude(conn, target, repo_id, marker, process_info or runs._process_info)
        canonical_target = detail["session_id"]
    else:
        client, detail = prepare_codex(conn, target, repo_id, marker, codex_connect or desktop_codex.connect)
        canonical_target = detail["thread_id"]

    try:
        with tx(conn):
            old = _request_row(conn, request_id)
            if old is not None:
                return _replay(old, kind, target, prompt_sha, origin)
            timestamp = now()
            conn.execute(
                "INSERT INTO desktop_requests(request_id,kind,target,actor,repo_id,prompt_sha256,prompt_chars,"
                "state,detail_json,created_at,updated_at,origin) VALUES(?,?,?,?,?,?,?,'SENDING',?,?,?,?)",
                (request_id, kind, canonical_target, actor, repo_id, prompt_sha, len(prompt),
                 canonical_json(detail).decode("utf-8"), timestamp, timestamp, origin),
            )

        try:
            if kind == "claude":
                extra = send_claude(path, token, detail, wire_prompt, claude_open or _open_claude_pipe)
            else:
                extra = send_codex(client, detail, wire_prompt)
        except SendFailedBeforeWrite as exc:
            error_detail = {"request_id": request_id}
            state = "FAILED"
            if isinstance(exc, SendOutcomeUnknown):
                state = "UNKNOWN"
                error_detail["state"] = state
            with tx(conn):
                conn.execute("UPDATE desktop_requests SET state=?, error=?, updated_at=? WHERE request_id=?",
                             (state, exc.code, now(), request_id))
            raise ValidationError(exc.code, detail=error_detail) from None

        with tx(conn):
            conn.execute("UPDATE desktop_requests SET state='SENT', detail_json=?, updated_at=? WHERE request_id=?",
                         (canonical_json(detail | extra).decode("utf-8"), now(), request_id))
            row = _request_row(conn, request_id)
            _enqueue_relay(conn, row, prompt)
        return _row_view(row) | {"replayed": False}
    finally:
        if client is not None:
            _close_codex(client)


def desktop_dispatch(conn, *, kind: str, target: str, prompt: str, request_id: str, actor: str,
                     process_info=None, claude_open=None, codex_connect=None) -> dict:
    if not prompt.strip():
        raise ValidationError("prompt_empty")
    if len(prompt) > WECHAT_MAX_PROMPT_CHARS:
        raise ValidationError("prompt_too_long", detail={"chars": len(prompt), "max": WECHAT_MAX_PROMPT_CHARS})
    if _request_row(conn, request_id) is None:
        since = (datetime.fromisoformat(now()) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        count = conn.execute(
            "SELECT COUNT(*) FROM desktop_requests WHERE origin='wechat' AND created_at>=?", (since,),
        ).fetchone()[0]
        if count >= WECHAT_DISPATCH_PER_HOUR:
            raise ValidationError("dispatch_rate_limited", detail={"limit": WECHAT_DISPATCH_PER_HOUR})
    result = desktop_send(conn, kind=kind, target=target, prompt=prompt, request_id=request_id, actor=actor,
                          origin="wechat", process_info=process_info, claude_open=claude_open,
                          codex_connect=codex_connect)
    return secretary_view(conn, result)


def message_claude(conn, *, target: str, prompt: str, request_id: str, actor: str,
                   process_info=None, claude_open=None) -> dict:
    if actor != "codex":
        raise ValidationError("actor_tool_not_allowed")
    if not prompt.strip():
        raise ValidationError("prompt_empty")
    if len(prompt) > WECHAT_MAX_PROMPT_CHARS:
        raise ValidationError("prompt_too_long", detail={"chars": len(prompt), "max": WECHAT_MAX_PROMPT_CHARS})
    if _request_row(conn, request_id) is None:
        since = (datetime.fromisoformat(now()) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        count = conn.execute(
            "SELECT COUNT(*) FROM desktop_requests WHERE origin='codex' AND created_at>=?", (since,),
        ).fetchone()[0]
        if count >= CODEX_MESSAGE_PER_HOUR:
            raise ValidationError("message_rate_limited", detail={"limit": CODEX_MESSAGE_PER_HOUR})
    return desktop_send(conn, kind="claude", target=target, prompt=prompt, request_id=request_id, actor=actor,
                        origin="codex", process_info=process_info, claude_open=claude_open)


def _enqueue_relay(conn, row, prompt: str) -> None:
    if row["origin"] == "wechat":
        return
    direction = {"opus": "Claude → Codex" if row["kind"] == "codex" else "Claude → Claude",
                 "gemini": "Gemini → Codex" if row["kind"] == "codex" else "Gemini → Claude",
                 "codex": "Codex → Claude"}.get(row["actor"])
    if direction is None:
        return
    detail = json.loads(row["detail_json"])
    confidential = confidential_cwd(conn, detail.get("cwd"))
    if confidential:
        name = "（机密仓库）"
        body = "（机密仓库，正文不经微信）"
    else:
        name = detail.get("name") or detail.get("thread_id", "")[:8] or row["target"][:8]
        body = prompt
    text = f"[LabOps · {direction}] {name}\n请求：{row['request_id']}\n{body}"
    conn.execute(
        "INSERT OR IGNORE INTO notifications(event_key,task_id,kind,text,status,created_at) "
        "VALUES(?,NULL,'agent_relay',?,'PENDING',?)",
        (f"relay:{row['request_id']}", text, now()),
    )


def confidential_cwd(conn, cwd: str | None) -> bool:
    if not cwd:
        return False
    target = os.path.normcase(os.path.realpath(cwd))
    for row in conn.execute("SELECT path FROM repos WHERE confidential=1"):
        root = os.path.normcase(os.path.realpath(row["path"]))
        if target == root or target.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def secretary_view(conn, payload: dict) -> dict:
    if "sessions" in payload or "threads" in payload:
        items = payload.get("sessions") or payload.get("threads") or []
        for item in items:
            if confidential_cwd(conn, item.get("cwd")):
                item["cwd"] = None
                if "name" in item:
                    item["name"] = None
                item["confidential"] = True
        return payload

    detail = payload.get("detail") or {}
    confidential = confidential_cwd(conn, detail.get("cwd"))
    result = {key: payload.get(key) for key in ("request_id", "kind", "target", "state", "error", "created_at",
                                               "updated_at", "phase", "hint", "replayed")}
    result["name"] = None if confidential else detail.get("name")
    result["reply"] = None if confidential else payload.get("reply")
    if confidential:
        result["confidential"] = True
    return result


def desktop_list(conn, *, kind: str, limit: int = 20, process_info=None) -> dict:
    if kind not in ("claude", "codex"):
        raise ValidationError("invalid_kind")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValidationError("invalid_limit")
    if kind == "claude":
        sessions = desktop_claude.list_sessions(_claude_home(), process_info=process_info or runs._process_info,
                                                limit=limit)
        for session in sessions:
            del session["messaging_socket_path"]
        return {"kind": kind, "sessions": sessions}
    return {"kind": kind, "threads": desktop_codex.list_threads(_codex_home(), limit),
            "note": "listed from rollout files; the thread must be open in Codex Desktop to receive"}


def _age_seconds(timestamp: str) -> float:
    return (datetime.fromisoformat(now()) - datetime.fromisoformat(timestamp)).total_seconds()


def _claude_status(detail, updated_at, process_info) -> dict:
    home = _claude_home()
    path = (Path(detail["transcript_path"]) if detail["transcript_path"]
            else desktop_claude.find_transcript(home, detail["session_id"]))
    scan = {"seen": False, "delivered_at": None, "reply": None}
    if path is not None and path.is_file():
        scan = desktop_claude.scan_transcript(path, detail["baseline_offset"], detail["marker"])
    sessions = desktop_claude.list_sessions(home, process_info=process_info, limit=50)
    live = next((row for row in sessions if row["pid"] == detail["pid"] and row["proc_start"] == detail["proc_start"]),
                None)
    if live is None or live["alive"] is not True:
        phase = "session_gone"
    elif not scan["seen"]:
        phase = "not_seen"
    elif live["status"] != "idle":
        phase = "running"
    else:
        phase = "completed"
    hint = "peer_message_may_be_held_or_refused" if phase == "not_seen" and _age_seconds(updated_at) > 300 else None
    return {"phase": phase, "delivered_at": scan["delivered_at"], "reply": scan["reply"], "hint": hint}


def _codex_status(detail, updated_at) -> dict:
    path = Path(detail["rollout_path"])
    if not path.is_file():
        return {"phase": "session_gone", "reply": None, "hint": None}
    scan = desktop_codex.scan_rollout(path, detail["baseline_offset"], detail["turn_id"])
    hint = "turn_not_started" if scan["phase"] == "not_seen" and _age_seconds(updated_at) > 120 else None
    return scan | {"hint": hint}


def desktop_status(conn, *, request_id: str, process_info=None) -> dict:
    row = _request_row(conn, request_id)
    if row is None:
        raise ValidationError("request_not_found")
    view = _row_view(row)
    if row["state"] in ("SENDING", "FAILED"):
        return view | {"phase": None}
    if row["kind"] == "claude":
        return view | _claude_status(view["detail"], row["updated_at"], process_info or runs._process_info)
    return view | _codex_status(view["detail"], row["updated_at"])
