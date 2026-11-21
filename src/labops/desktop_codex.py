import json
import queue
import re
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import desktop
from .errors import ValidationError

_MAX_FRAME_BYTES = 32 * 1024 * 1024


class IpcTimeout(Exception):
    pass


class IpcError(Exception):
    pass


class IpcClient:
    def __init__(self, stream):
        self.stream = stream
        self.client_id: str | None = None
        self.last_response: dict = {}  # full envelope of the last successful response
        self._queue = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            part = self.stream.read(size - len(data))
            if not part:
                break
            data.extend(part)
        return bytes(data)

    def _reader(self) -> None:
        try:
            while True:
                header = self._read_exact(4)
                if len(header) < 4:
                    break
                length = struct.unpack("<I", header)[0]
                if length > _MAX_FRAME_BYTES:
                    self._queue.put(("error", "frame_too_large"))
                    return
                body = self._read_exact(length)
                if len(body) < length:
                    break
                try:
                    frame = json.loads(body.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(frame, dict):
                    self._queue.put(("frame", frame))
        except OSError:
            pass
        self._queue.put(("eof", None))

    def _send(self, frame: dict) -> None:
        body = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        payload = struct.pack("<I", len(body)) + body
        try:
            if self.stream.write(payload) != len(payload):
                raise OSError("short IPC write")
        except OSError:
            raise IpcError("write") from None

    def request(self, method, params, *, version=1, target=None, timeout=20.0) -> dict:
        request_id = str(uuid4())
        envelope = {"type": "request", "requestId": request_id, "method": method, "params": params,
                    "sourceClientId": self.client_id or "initializing-client", "version": version}
        if target:
            envelope["targetClientId"] = target
        self._send(envelope)

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IpcTimeout
            try:
                kind, frame = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise IpcTimeout from None
            if kind != "frame":
                raise IpcError(frame or "closed")
            if frame.get("type") == "response" and frame.get("requestId") == request_id:
                if frame.get("resultType") == "success":
                    self.last_response = frame
                    return frame.get("result") or {}
                raise IpcError(str(frame.get("error"))[:200])
            if frame.get("type") == "client-discovery-request":
                # Codex 26.1111.9664 asks every client whether it can handle a routed request and
                # waits up to timeoutMs for the answer; we never handle any (DESK-3).
                self._send({"type": "client-discovery-response", "requestId": frame.get("requestId"),
                            "response": {"canHandle": False}})
                continue
            if frame.get("type") == "request":
                response = {"type": "response", "requestId": frame.get("requestId"),
                            "sourceClientId": self.client_id}
                if frame.get("method") == "client-discovery-request":
                    response.update(resultType="success", result={"canHandle": False})
                else:
                    response.update(resultType="error", error="no-handler-for-request")
                self._send(response)


class _PolledPipe:
    """Duplex stream over a synchronous Windows pipe handle.

    Windows serializes I/O on a handle opened without FILE_FLAG_OVERLAPPED: while the reader
    thread sits in a blocking ReadFile, WriteFile from the caller cannot proceed. Reads therefore
    poll PeekNamedPipe and only call ReadFile for bytes already available, so no read ever blocks.
    """

    POLL_S = 0.01

    def __init__(self, raw):
        import msvcrt

        self._raw = raw
        self._handle = msvcrt.get_osfhandle(raw.fileno())
        self._closed = False

    def read(self, size: int) -> bytes:
        import _winapi

        while not self._closed:
            try:
                available, _left = _winapi.PeekNamedPipe(self._handle, 0)
                if available:
                    data, _err = _winapi.ReadFile(self._handle, min(size, available))
                    return data
            except OSError:
                return b""
            time.sleep(self.POLL_S)
        return b""

    def write(self, data: bytes) -> int:
        return self._raw.write(data)

    def close(self) -> None:
        self._closed = True
        self._raw.close()


def connect(pipe_path: str) -> IpcClient:
    try:
        stream = open(pipe_path, "r+b", buffering=0)
    except OSError:
        raise ValidationError("codex_ipc_unavailable") from None
    return IpcClient(_PolledPipe(stream))


def find_rollout(codex_home: Path, thread_id: str) -> Path | None:
    """Return the rollout file Codex is currently appending to for this thread.

    Long-lived threads are split: older segments are compressed to .jsonl.zst and the active
    one is named rollout-<ts>-<thread>_<segment>.jsonl. Only uncompressed files can receive new
    events, so pick the newest of those whose first line names this thread.
    """
    name_re = re.compile(rf"^rollout-.+-{re.escape(thread_id)}(?:_[0-9a-f-]{{36}})?\.jsonl$", re.I)
    candidates = [path for path in (codex_home / "sessions").rglob(f"rollout-*-{thread_id}*.jsonl")
                  if name_re.fullmatch(path.name)]
    for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
        if str(read_session_meta(path).get("id") or "").lower() == thread_id.lower():
            return path
    return None


def normalize_thread_target(target: str) -> str:
    """Accept codex://threads/<id> links as well as the bare thread id."""
    value = target.strip()
    match = re.match(r"^codex://threads/([^/?#]+)", value, re.I)
    return (match[1] if match else value).lower()


def read_session_meta(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as source:
            record = json.loads(source.readline())
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return {}
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return {}
    return {key: payload.get(key) for key in ("id", "cwd", "cli_version")}


def list_threads(codex_home: Path, limit: int) -> list[dict]:
    paths = (codex_home / "sessions").rglob("rollout-*.jsonl")
    recent = sorted(((path.stat().st_mtime, path) for path in paths), key=lambda item: item[0], reverse=True)
    threads = []
    seen = set()
    for modified, path in recent:
        if len(threads) >= limit:
            break
        meta = read_session_meta(path)
        thread_id = meta.get("id") or path.stem[-36:]
        if thread_id in seen:  # a split thread has several segment files; list it once
            continue
        seen.add(thread_id)
        threads.append({"thread_id": thread_id, "cwd": meta.get("cwd"),
                        "cli_version": meta.get("cli_version"),
                        "updated_at": datetime.fromtimestamp(modified, timezone.utc).isoformat().replace("+00:00", "Z")})
    return threads


def extract_turn_id(result) -> str | None:
    if not isinstance(result, dict):
        return None
    if isinstance(result.get("result"), dict):
        result = result["result"]
    turn_id = result.get("turnId")
    if not turn_id and isinstance(result.get("turn"), dict):
        turn_id = result["turn"].get("id")
    return turn_id if isinstance(turn_id, str) and turn_id else None


def scan_rollout(path: Path, baseline_offset: int, turn_id: str | None) -> dict:
    phase = "not_seen"
    reply = None
    for record in desktop.read_complete_lines(path, baseline_offset):
        payload = record.get("payload")
        if record.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        event = payload.get("type")
        observed = payload.get("turn_id")
        if turn_id is None and event == "task_started" and isinstance(observed, str):
            turn_id = observed
        if turn_id is None or observed != turn_id:
            continue
        if event == "task_started" and phase == "not_seen":
            phase = "running"
        elif event == "task_complete":
            phase = "completed"
            reply = payload.get("last_agent_message")
        elif event in ("turn_aborted", "turn_interrupted"):
            phase = "interrupted"
    return {"phase": phase, "reply": reply, "turn_id_observed": turn_id}
