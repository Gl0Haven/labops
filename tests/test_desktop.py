import contextlib
import hashlib
import io
import json
import os
import queue
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops import desktop as service, desktop_claude as claude, desktop_codex as codex
from labops.cli import build_parser, dispatch
from labops.db import connect
from labops.errors import IdempotencyConflict, UsageError, ValidationError
from labops.runs import ProcessInfoUnavailable

PID = 24128
SESSION_ID = "4d367b70-fb4d-4dde-8bae-d939b411d772"
HOST_ID = f"local_{SESSION_ID}"
THREAD_ID = "2d3404de-e07c-451d-aafc-4acaaaf3773c"
PROC_START = "134080633460000000"
PIPE = r"\\.\pipe\LOCAL\cc-msg-fc134a6574a6a1298cf65667900efbc7"
TOKEN = "a" * 32
PROMPT = '请回复 "OK"\n第二行'


def append_jsonl(path, *records):
    with path.open("ab") as handle:
        for record in records:
            handle.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))


def transcript_record(kind, content, **fields):
    return {"type": kind, "message": {"content": content}, **fields}


def rollout_event(event, turn_id="t1", **fields):
    return {"type": "event_msg", "payload": {"type": event, "turn_id": turn_id, **fields}}


@contextlib.contextmanager
def desktop_fixture():
    with tempfile.TemporaryDirectory(prefix="labops-desktop-") as directory:
        root = Path(directory)
        home = root / "labops"
        with patch.dict(os.environ, {"LABOPS_HOME": str(home), "LABOPS_CLAUDE_HOME": str(root / "claude"),
                                     "LABOPS_CODEX_HOME": str(root / "codex"),
                                     "LABOPS_CODEX_IPC_PIPE": "fake-codex-ipc"}):
            dispatch(build_parser().parse_args(["init", "--test-instance"]))
            with contextlib.closing(connect()) as conn:
                yield DesktopFixture(root, conn)


class DesktopFixture:
    def __init__(self, root, conn):
        self.root = root
        self.conn = conn
        self.claude_home = root / "claude"
        self.sessions = self.claude_home / "sessions"
        self.sessions.mkdir(parents=True)
        self.registration = {
            "pid": PID, "sessionId": SESSION_ID, "hostSessionId": HOST_ID, "cwd": str(root),
            "version": "2.1.fake", "entrypoint": "claude-desktop", "status": "idle", "name": "desk",
            "messagingSocketPath": PIPE, "procStart": PROC_START,
        }
        self.registration_path = self.sessions / f"{PID}.json"
        self.write_registration()
        self.key = self.sessions / claude.canonical_pipe_key_name(PID, PIPE)
        self.key.write_text(json.dumps({"peerToken": TOKEN, "procStartFt": PROC_START,
                                        "pidDomain": "win32:fixture"}), encoding="utf-8")
        self.transcript = self.claude_home / "projects" / "fake-repo" / f"{SESSION_ID}.jsonl"
        self.transcript.parent.mkdir(parents=True)
        append_jsonl(self.transcript, transcript_record("assistant", "old Claude reply"))

        self.codex_home = root / "codex"
        self.rollout = (self.codex_home / "sessions" / "2026" / "11" / "18"
                        / f"rollout-2026-11-18T12-00-00-{THREAD_ID}.jsonl")
        self.rollout.parent.mkdir(parents=True)
        append_jsonl(self.rollout, {"type": "session_meta",
                                    "payload": {"id": THREAD_ID, "cwd": str(root), "cli_version": "fake"}})

    def write_registration(self):
        self.registration_path.write_text(json.dumps(self.registration), encoding="utf-8")

    def process_info(self, pid):
        if pid == PID:
            return {"id": pid, "start_utc": claude.filetime_to_iso(PROC_START), "command_line": ""}

    def send(self, kind, *, target=None, request_id="request-123", prompt=PROMPT, **kwargs):
        return service.desktop_send(self.conn, kind=kind, target=target or (HOST_ID if kind == "claude" else THREAD_ID),
                                    request_id=request_id, prompt=prompt, actor="opus",
                                    process_info=self.process_info, **kwargs)

    def status(self, request_id="request-123"):
        return service.desktop_status(self.conn, request_id=request_id, process_info=self.process_info)


class ClaudeSink(io.BytesIO):
    payload: bytes

    def close(self):
        if not self.closed:
            self.payload = self.getvalue()
        super().close()


def encode_frame(frame):
    body = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return struct.pack("<I", len(body)) + body


class FakeCodexStream:
    def __init__(self, *, mode="success"):
        self.mode = mode
        self.from_client = queue.Queue()
        self.to_client = queue.Queue()
        self._buffer = bytearray()
        self.closed = False

    def read(self, size):
        if not self._buffer:
            data = self.to_client.get()
            if data is None:
                return b""
            self._buffer.extend(data)
        count = min(size, 7, len(self._buffer))
        chunk = self._buffer[:count]
        del self._buffer[:count]
        return bytes(chunk)

    def write(self, frame):
        self.from_client.put(frame)
        if frame[:4] != struct.pack("<I", len(frame) - 4):
            raise AssertionError("expected little-endian frame prefix")
        request = json.loads(frame[4:])
        if request["type"] in ("response", "client-discovery-response"):
            return len(frame)

        response = {"type": "response", "requestId": request["requestId"], "resultType": "success"}
        method = request["method"]
        if method == "initialize":
            response["result"] = {"clientId": "client-fixture"}
        elif method == "thread-owner-discovery":
            self.to_client.put(encode_frame({"type": "broadcast", "method": "ignore-me"}))
            self.to_client.put(encode_frame({"type": "request", "requestId": "discover-fixture",
                                            "method": "client-discovery-request"}))
            self.to_client.put(encode_frame({"type": "request", "requestId": "unknown-fixture",
                                            "method": "unknown-method"}))
            # Shape observed on live Codex 26.1111.2228: owner on the envelope, not in result.
            response["result"] = {"supportsUntrustedAppInput": True}
            if self.mode != "no_owner":
                response["handledByClientId"] = "owner-fixture"
        elif method == "thread-follower-start-turn":
            if self.mode == "timeout":
                return len(frame)
            if self.mode == "discovery_first":
                # New-style probe seen on Codex 26.1111.9664 before the start-turn answer.
                self.to_client.put(encode_frame({"type": "client-discovery-request", "requestId": "probe-new",
                                                 "request": {"type": "request", "method": "ide-context",
                                                             "version": 0}, "timeoutMs": 5000}))
            if self.mode == "failure":
                response.update(resultType="error", error="rejected")
            elif self.mode == "turn_timeout":
                response.update(resultType="error", error="thread-follower-start-turn-timeout")
            else:
                response["result"] = {"result": {"turnId": "t1"}}
        else:
            raise AssertionError(f"unexpected fake request {method}")
        self.to_client.put(encode_frame(response))
        return len(frame)

    def close(self):
        self.closed = True
        self.to_client.put(None)

    def take_frames(self):
        result = []
        while not self.from_client.empty():
            result.append(self.from_client.get_nowait())
        return result


class FastIpcClient(codex.IpcClient):
    def request(self, method, params, *, version=1, target=None, timeout=20.0):
        return super().request(method, params, version=version, target=target, timeout=min(timeout, 0.2))


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.fixture = self.enterContext(desktop_fixture())

    def test_canonical_pipe_key_name(self):
        canonical = r"\\.\pipe\local\cc-msg-fc134a6574a6a1298cf65667900efbc7"
        expected = f"{PID}.{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}.key"
        self.assertEqual(claude.canonical_pipe_key_name(PID, PIPE), expected)
        self.assertEqual(claude.canonical_pipe_key_name(PID, PIPE.replace("\\", "/")), expected)
        with self.assertRaisesRegex(ValidationError, "key_unusable"):
            claude.canonical_pipe_key_name(PID, "not-a-pipe")

    def test_build_lines_preserves_unicode_quotes_and_newline(self):
        data = claude.build_lines(TOKEN, SESSION_ID, PROMPT, "id-one", "uuid-two")
        self.assertEqual(data.count(b"\n"), 2)
        auth, message = [json.loads(line) for line in data.splitlines()]
        self.assertEqual(auth, {"type": "auth", "token": TOKEN})
        self.assertEqual(message["message"], {"role": "user", "content": PROMPT})

    def test_list_and_resolve_claude(self):
        fixture = self.fixture
        sessions = claude.list_sessions(fixture.claude_home, process_info=fixture.process_info, limit=50)
        for key in (SESSION_ID, HOST_ID, "desk"):
            self.assertEqual(claude.resolve_target(sessions, key)["session_id"], SESSION_ID)
        public = service.desktop_list(fixture.conn, kind="claude", process_info=fixture.process_info)
        self.assertNotIn("messaging_socket_path", public["sessions"][0])
        self.assertTrue(public["sessions"][0]["alive"])
        with self.assertRaisesRegex(ValidationError, "target_not_found"):
            claude.resolve_target(sessions, "Desk")
        with self.assertRaisesRegex(ValidationError, "target_ambiguous") as ambiguous:
            claude.resolve_target(sessions * 2, "desk")
        self.assertEqual(len(ambiguous.exception.detail["candidates"]), 2)
        with self.assertRaisesRegex(ValidationError, "target_not_desktop"):
            claude.resolve_target([sessions[0] | {"entrypoint": "cli"}], SESSION_ID)
        for alive in (False, None):
            with self.assertRaisesRegex(ValidationError, "target_not_running") as dead:
                claude.resolve_target([sessions[0] | {"alive": alive}], SESSION_ID)
            self.assertEqual(dead.exception.detail, {"pid": PID, "alive": alive})

        def unavailable(pid):
            raise ProcessInfoUnavailable()
        offline = claude.list_sessions(fixture.claude_home, process_info=unavailable, limit=50)
        self.assertIsNone(offline[0]["alive"])

    def test_key_validation_does_not_expose_secret(self):
        fixture = self.fixture
        session = claude.list_sessions(fixture.claude_home, process_info=fixture.process_info, limit=50)[0]
        self.assertEqual(claude.read_peer_token(fixture.claude_home, session), TOKEN)
        for raw, reason in ((json.dumps({"peerToken": TOKEN + "!"}), "token"),
                            (json.dumps({"peerToken": TOKEN, "procStartFt": "0"}), "stale"),
                            (json.dumps({"peerToken": TOKEN}).ljust(4097), "read")):
            with self.subTest(reason=reason):
                fixture.key.write_text(raw, encoding="utf-8")
                with self.assertRaisesRegex(ValidationError, "key_unusable") as error:
                    claude.read_peer_token(fixture.claude_home, session)
                self.assertEqual(error.exception.detail, {"reason": reason})
                self.assertNotIn(TOKEN, str(error.exception))

    def test_claude_send_replays_and_status_uses_baseline(self):
        fixture = self.fixture
        sink = ClaudeSink()
        attempts = []

        def open_pipe(path):
            attempts.append(path)
            self.assertEqual(fixture.status()["state"], "SENDING")
            self.assertIsNone(fixture.status()["phase"])
            return sink

        sent = fixture.send("claude", claude_open=open_pipe)
        self.assertEqual((sent["state"], sent["replayed"]), ("SENT", False))
        self.assertEqual(set(sent["detail"]), {"session_id", "host_session_id", "pid", "proc_start", "cwd", "name",
                                              "claude_version", "transcript_path", "baseline_offset", "marker", "msg_id"})
        self.assertNotIn(TOKEN, json.dumps(sent))
        saved = fixture.conn.execute("SELECT * FROM desktop_requests").fetchone()
        self.assertEqual(saved["prompt_sha256"], hashlib.sha256(PROMPT.encode("utf-8")).hexdigest())
        self.assertNotIn(PROMPT, str(tuple(saved)))
        self.assertNotIn(TOKEN, str(tuple(saved)))
        message = json.loads(sink.payload.splitlines()[1])
        self.assertEqual(message["session_id"], SESSION_ID)
        self.assertEqual(message["msg_id"], sent["detail"]["msg_id"])
        self.assertEqual(message["message"]["content"], f"[labops:request-123]\n{PROMPT}")
        replayed = fixture.send("claude", target="desk", claude_open=open_pipe)
        self.assertTrue(replayed["replayed"])
        self.assertEqual(attempts, [PIPE])
        with self.assertRaises(IdempotencyConflict):
            fixture.send("claude", prompt="different", claude_open=open_pipe)
        self.assertEqual(fixture.status()["phase"], "not_seen")
        fixture.registration["status"] = "busy"
        fixture.write_registration()
        append_jsonl(fixture.transcript, transcript_record("user", message["message"]["content"]))
        self.assertEqual(fixture.status()["phase"], "running")
        append_jsonl(fixture.transcript, transcript_record("assistant", "reply"))
        fixture.registration["status"] = "idle"
        fixture.write_registration()
        self.assertEqual((fixture.status()["phase"], fixture.status()["reply"]), ("completed", "reply"))

    def test_claude_busy_retry(self):
        fixture = self.fixture
        attempts = []

        def busy_twice(path):
            attempts.append(path)
            if len(attempts) < 3:
                error = OSError("busy")
                error.winerror = 231
                raise error
            return ClaudeSink()

        self.assertEqual(fixture.send("claude", claude_open=busy_twice)["state"], "SENT")
        self.assertEqual(attempts, [PIPE] * 3)

    def test_claude_failed_open_and_unknown_write(self):
        fixture = self.fixture
        calls = []
        fixture_key = fixture.key.read_text(encoding="utf-8")

        def failed_open(path):
            error = OSError("busy")
            error.winerror = 231
            raise error

        with self.assertRaisesRegex(ValidationError, "pipe_unavailable") as error:
            fixture.send("claude", request_id="failed-123", claude_open=failed_open)
        self.assertEqual(error.exception.detail, {"request_id": "failed-123"})
        self.assertEqual((fixture.status("failed-123")["state"], fixture.status("failed-123")["phase"]), ("FAILED", None))

        class UncertainSink(ClaudeSink):
            def write(self, payload):
                raise OSError(fixture_key)

        def open_sink(path):
            calls.append(path)
            return UncertainSink()

        with self.assertRaisesRegex(ValidationError, "pipe_unavailable") as unknown:
            fixture.send("claude", claude_open=open_sink)
        self.assertEqual(unknown.exception.detail, {"request_id": "request-123", "state": "UNKNOWN"})
        self.assertNotIn(TOKEN, str(unknown.exception))
        self.assertTrue(fixture.send("claude", claude_open=open_sink)["replayed"])
        self.assertEqual(calls, [PIPE])
        self.assertEqual(fixture.status()["state"], "UNKNOWN")

    def test_scan_transcript_stops_at_next_human_and_ignores_partial_line(self):
        path = self.fixture.transcript
        append_jsonl(path, transcript_record("user", "[labops:request-123] old"))
        offset = path.stat().st_size
        append_jsonl(path, transcript_record("assistant", "before"),
                     transcript_record("user", "[labops:request-123]\nhello", timestamp="stamp"),
                     transcript_record("assistant", "sidechain", isSidechain=True),
                     transcript_record("user", [{"type": "tool_result", "content": "tool result"}]),
                     transcript_record("assistant", [{"type": "text", "text": "main reply"},
                                                     {"type": "tool_use"}, {"type": "text", "text": "line two"}]))
        with path.open("ab") as writer:
            writer.write(json.dumps(transcript_record("assistant", "partial")).encode("utf-8"))
        expected = {"seen": True, "delivered_at": "stamp", "reply": "main reply\nline two"}
        self.assertEqual(claude.scan_transcript(path, offset, "[labops:request-123]"), expected)

        # Finish the malformed tail as one skipped line, then add a new user turn.
        with path.open("ab") as writer:
            writer.write(b"!\n")
        append_jsonl(path, transcript_record("user", "next person"),
                     transcript_record("assistant", "wrong reply"))
        self.assertEqual(claude.scan_transcript(path, offset, "[labops:request-123]"), expected)

    def test_codex_send_ipc_frames_replay_and_status(self):
        fixture = self.fixture
        fake = FakeCodexStream()
        connections = []

        def connector(path):
            connections.append(path)
            return codex.IpcClient(fake)

        result = fixture.send("codex", target=THREAD_ID.upper(), codex_connect=connector)
        self.assertTrue(fake.closed)
        self.assertEqual((result["target"], result["state"], result["detail"]["turn_id"]), (THREAD_ID, "SENT", "t1"))
        self.assertEqual(set(result["detail"]), {"thread_id", "owner_client_id", "turn_id", "client_user_message_id",
                                                "rollout_path", "baseline_offset", "marker", "cli_version", "cwd"})
        initialize, discovery, refusal, no_handler, start = [json.loads(frame[4:]) for frame in fake.take_frames()]
        self.assertEqual((initialize["method"], initialize["sourceClientId"], initialize["version"]),
                         ("initialize", "initializing-client", 1))
        self.assertEqual(initialize["params"], {"clientType": "labops"})
        self.assertEqual((discovery["method"], discovery["sourceClientId"]), ("thread-owner-discovery", "client-fixture"))
        self.assertEqual(discovery["params"], {"hostId": "local", "conversationId": THREAD_ID})
        self.assertEqual(refusal, {"type": "response", "requestId": "discover-fixture", "resultType": "success",
                                   "result": {"canHandle": False}, "sourceClientId": "client-fixture"})
        self.assertEqual((no_handler["resultType"], no_handler["error"]), ("error", "no-handler-for-request"))
        self.assertEqual((start["method"], start["version"], start["targetClientId"]),
                         ("thread-follower-start-turn", 2, "owner-fixture"))
        self.assertEqual(start["params"], {
            "conversationId": THREAD_ID,
            "turnStart": {
                "request": {"threadId": THREAD_ID, "input": [{"type": "text", "text": f"[labops:request-123]\n{PROMPT}",
                                                              "text_elements": []}],
                            "attachments": [], "clientUserMessageId": result["detail"]["client_user_message_id"],
                            "additionalContext": {}},
                "context": {"inheritThreadSettings": True},
            },
            "isSteering": False,
        })
        self.assertTrue(fixture.send("codex", codex_connect=connector)["replayed"])
        self.assertEqual(connections, ["fake-codex-ipc"])
        append_jsonl(fixture.rollout, rollout_event("task_started", "unrelated"), rollout_event("task_started"))
        self.assertEqual(fixture.status()["phase"], "running")
        append_jsonl(fixture.rollout, rollout_event("task_complete", last_agent_message="codex reply"))
        self.assertEqual((fixture.status()["phase"], fixture.status()["reply"]), ("completed", "codex reply"))

    def test_codex_error_timeout_and_no_owner(self):
        fixture = self.fixture
        for mode, expected_error, state in (("failure", "codex_start_turn_failed", "FAILED"),
                                            ("timeout", "codex_start_turn_failed", "UNKNOWN"),
                                            ("no_owner", "codex_thread_not_open", None)):
            with self.subTest(mode=mode):
                fake = FakeCodexStream(mode=mode)
                client = FastIpcClient(fake)
                request_id = f"request-{mode}"
                with self.assertRaisesRegex(ValidationError, expected_error):
                    fixture.send("codex", request_id=request_id, codex_connect=lambda _: client)
                self.assertTrue(fake.closed)
                if state is None:
                    with self.assertRaisesRegex(ValidationError, "request_not_found"):
                        fixture.status(request_id)
                else:
                    self.assertEqual(fixture.status(request_id)["state"], state)
                    replayed = fixture.send("codex", request_id=request_id, codex_connect=self.fail)
                    self.assertTrue(replayed["replayed"])

    def test_codex_ipc_rejects_oversized_frame(self):
        fake = FakeCodexStream()
        self.addCleanup(fake.close)
        fake.to_client.put(struct.pack("<I", 32 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(codex.IpcError, "frame_too_large"):
            codex.IpcClient(fake).request("initialize", {}, timeout=0.2)

    def test_scan_rollout_falls_back_to_first_new_turn_and_handles_abort(self):
        path = self.fixture.rollout
        append_jsonl(path, rollout_event("task_started", "old"))
        offset = path.stat().st_size
        append_jsonl(path, rollout_event("task_started", "new"), rollout_event("task_complete", "other"))
        self.assertEqual(codex.scan_rollout(path, offset, None),
                         {"phase": "running", "reply": None, "turn_id_observed": "new"})
        append_jsonl(path, rollout_event("turn_aborted", "new"))
        self.assertEqual(codex.scan_rollout(path, offset, "new"),
                         {"phase": "interrupted", "reply": None, "turn_id_observed": "new"})
        self.assertEqual(codex.extract_turn_id({"turn": {"id": "alternate"}}), "alternate")

    def test_validation_repo_scope_and_codex_list(self):
        fixture = self.fixture
        with self.assertRaisesRegex(ValidationError, "invalid_kind"):
            fixture.send("unknown")
        with self.assertRaisesRegex(ValidationError, "invalid_request_id"):
            fixture.send("codex", request_id="short")
        with self.assertRaisesRegex(ValidationError, "prompt_empty"):
            fixture.send("codex", prompt=" \n")
        with self.assertRaisesRegex(ValidationError, "prompt_too_long"):
            fixture.send("codex", prompt="X" * 20001)
        with self.assertRaisesRegex(ValidationError, "invalid_limit"):
            service.desktop_list(fixture.conn, kind="codex", limit=51)
        with self.assertRaisesRegex(ValidationError, "codex_thread_unknown") as invalid:
            fixture.send("codex", target="invalid")
        self.assertEqual(invalid.exception.detail, {"reason": "format"})
        with self.assertRaisesRegex(ValidationError, "codex_thread_unknown") as missing:
            fixture.send("codex", target=SESSION_ID)
        self.assertEqual(missing.exception.detail, {"reason": "no_rollout"})
        fixture.conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('other',?,'t')",
                             (str(fixture.root / "other"),))
        for kind in ("claude", "codex"):
            with self.assertRaisesRegex(ValidationError, "target_repo_mismatch"):
                fixture.send(kind, repo_id="other")
        self.assertEqual(fixture.conn.execute("SELECT COUNT(*) FROM desktop_requests").fetchone()[0], 0)
        result = service.desktop_list(fixture.conn, kind="codex")
        self.assertEqual(result["threads"][0]["thread_id"], THREAD_ID)

    def test_cli_requires_one_prompt_and_reads_status(self):
        fixture = self.fixture
        parser = build_parser()
        send = ["desktop", "send", "--kind", "claude", "--target", SESSION_ID, "--request-id", "request-123"]
        with self.assertRaises(UsageError):
            parser.parse_args(send)
        with self.assertRaises(UsageError):
            parser.parse_args(send + ["--prompt", "x", "--prompt-file", "file.txt"])
        def offline_pipe(_):
            raise OSError("fixture pipe unavailable")

        with self.assertRaisesRegex(ValidationError, "pipe_unavailable"):
            fixture.send("claude", claude_open=offline_pipe)
        status = dispatch(parser.parse_args(["desktop", "status", "--request-id", "request-123"]))
        self.assertEqual((status["kind"], status["state"], status["phase"]), ("claude", "FAILED", None))

    @unittest.skipUnless(os.name == "nt", "Windows named pipe")
    def test_codex_connect_writes_while_reader_waits_on_real_pipe(self):
        # Regression for DESK-1: a private local pipe, never a desktop pipe. A blocking ReadFile on a
        # synchronous handle used to hold up WriteFile, so initialize never reached the server.
        import _winapi
        import threading
        import time
        import uuid

        name = "\\\\.\\pipe\\labops-test-" + uuid.uuid4().hex
        server_handle = _winapi.CreateNamedPipe(name, _winapi.PIPE_ACCESS_DUPLEX, _winapi.PIPE_WAIT,
                                                1, 65536, 65536, 0, _winapi.NULL)
        self.addCleanup(_winapi.CloseHandle, server_handle)
        received = []

        def read_frame():
            header, _ = _winapi.ReadFile(server_handle, 4)
            body, _ = _winapi.ReadFile(server_handle, struct.unpack("<I", header)[0])
            return json.loads(body)

        def server():
            _winapi.ConnectNamedPipe(server_handle, False)
            request = read_frame()
            received.append(request["method"])
            _winapi.WriteFile(server_handle, encode_frame({"type": "request", "requestId": "probe",
                                                          "method": "client-discovery-request"}))
            received.append(read_frame())
            _winapi.WriteFile(server_handle, encode_frame({"type": "response", "requestId": request["requestId"],
                                                          "resultType": "success", "result": {"clientId": "c1"}}))

        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        client = codex.connect(name)
        self.addCleanup(client.stream.close)
        time.sleep(0.2)  # let the reader thread start waiting before the first write
        outcome = {}
        caller = threading.Thread(target=lambda: outcome.update(
            result=client.request("initialize", {"clientType": "labops"}, timeout=5)), daemon=True)
        caller.start()
        caller.join(10)  # a blocked WriteFile ignores the request timeout, so bound it here
        self.assertFalse(caller.is_alive(), "request blocked: write held up by the pending read")
        self.assertEqual(outcome.get("result"), {"clientId": "c1"})
        thread.join(5)
        self.assertEqual(received[0], "initialize")
        self.assertEqual((received[1]["requestId"], received[1]["result"]), ("probe", {"canHandle": False}))

    def test_codex_start_turn_timeout_error_is_unknown_not_failed(self):
        # DESK-3: the desktop answered "thread-follower-start-turn-timeout" although the turn ran.
        fake = FakeCodexStream(mode="turn_timeout")
        client = FastIpcClient(fake)
        with self.assertRaisesRegex(ValidationError, "codex_start_turn_failed"):
            self.fixture.send("codex", request_id="request-turn-timeout", codex_connect=lambda _: client)
        self.assertEqual(self.fixture.status("request-turn-timeout")["state"], "UNKNOWN")
        replayed = self.fixture.send("codex", request_id="request-turn-timeout", codex_connect=self.fail)
        self.assertTrue(replayed["replayed"])

    def test_codex_answers_new_style_client_discovery_request(self):
        fake = FakeCodexStream(mode="discovery_first")
        result = self.fixture.send("codex", request_id="request-discovery",
                                   codex_connect=lambda _: codex.IpcClient(fake))
        self.assertEqual(result["state"], "SENT")
        answers = [json.loads(frame[4:]) for frame in fake.take_frames()
                   if json.loads(frame[4:])["type"] == "client-discovery-response"]
        self.assertEqual(answers, [{"type": "client-discovery-response", "requestId": "probe-new",
                                    "response": {"canHandle": False}}])
