import argparse
import json
import secrets
import sys
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from labops import desktop, desktop_codex
from labops.db import connect, migrate
from labops.errors import LabOpsError


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def offline() -> int:
    from tests.test_desktop import (TOKEN, ClaudeSink, FakeCodexStream, append_jsonl, desktop_fixture,
                                    rollout_event, transcript_record)

    with desktop_fixture() as fixture:
        claude_targets = desktop.desktop_list(fixture.conn, kind="claude", process_info=fixture.process_info)
        codex_targets = desktop.desktop_list(fixture.conn, kind="codex")
        claude_target = claude_targets["sessions"][0]["session_id"]
        codex_target = codex_targets["threads"][0]["thread_id"]
        sink = ClaudeSink()
        fake = FakeCodexStream()
        claude_id = "smoke-claude"
        codex_id = "smoke-codex"
        claude_send = fixture.send("claude", target=claude_target, request_id=claude_id, claude_open=lambda _: sink)
        codex_send = fixture.send("codex", target=codex_target, request_id=codex_id,
                                  codex_connect=lambda _: desktop_codex.IpcClient(fake))
        auth, user_message = sink.payload.splitlines(keepends=True)
        append_jsonl(fixture.transcript,
                     transcript_record("user", json.loads(user_message)["message"]["content"], timestamp="fixture"),
                     transcript_record("assistant", "Claude fixture reply"))
        append_jsonl(fixture.rollout, rollout_event("task_started"),
                     rollout_event("task_complete", last_agent_message="Codex fixture reply"))
        claude_status = fixture.status(claude_id)
        codex_status = fixture.status(codex_id)

        def must_not_send(_):
            raise AssertionError("replay reached transport")

        claude_replay = fixture.send("claude", target=claude_target, request_id=claude_id, claude_open=must_not_send)
        codex_replay = fixture.send("codex", target=codex_target, request_id=codex_id, codex_connect=must_not_send)
        start_frame = next(frame for frame in fake.take_frames()
                           if json.loads(frame[4:]).get("method") == "thread-follower-start-turn")
        claude_sample = (auth.replace(TOKEN.encode("ascii"), b"<redacted>") + user_message).decode("utf-8")
        codex_sample = start_frame[4:].decode("utf-8")
        no_token = TOKEN not in _json([claude_send, codex_send, claude_status, codex_status, claude_replay,
                                       codex_replay, claude_targets, codex_targets, claude_sample, codex_sample])
        expected_turns = (
            claude_send["state"] == codex_send["state"] == "SENT"
            and claude_status["phase"] == codex_status["phase"] == "completed"
            and claude_status["reply"] == "Claude fixture reply"
            and codex_status["reply"] == "Codex fixture reply"
        )
        replayed = claude_replay["replayed"] and codex_replay["replayed"]
        ok = expected_turns and replayed and no_token
        print(_json({"fake_claude_utf8": claude_sample}))
        print(_json({"fake_codex_prefix_hex": start_frame[:4].hex(" "), "fake_codex_utf8": codex_sample}))
        print(_json({"ok": ok, "claude": {"send": claude_send["state"], "phase": claude_status["phase"]},
                     "codex": {"send": codex_send["state"], "phase": codex_status["phase"]},
                     "replay": replayed, "no_token_in_output": no_token}))
        return 0 if ok else 1


def live(kind: str, target: str, wait_seconds: int) -> int:
    nonce = f"{secrets.randbelow(1_000_000):06d}"
    request_id = f"smoke-{uuid4()}"
    with closing(connect()) as conn:
        migrate(conn)
        desktop.desktop_send(conn, kind=kind, target=target, request_id=request_id, actor="opus",
                             prompt=f"请只回复：LABOPS-PING-{nonce}")
        deadline = time.monotonic() + wait_seconds
        while True:
            status = desktop.desktop_status(conn, request_id=request_id)
            remaining = deadline - time.monotonic()
            if status["phase"] in {"completed", "interrupted", "session_gone"} or remaining <= 0:
                print(_json(status))
                reply = status.get("reply")
                return 0 if status["phase"] == "completed" and isinstance(reply, str) and nonce in reply else 1
            time.sleep(min(5, remaining))


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--kind", choices=("claude", "codex"))
    parser.add_argument("--target")
    parser.add_argument("--wait", type=int, default=180)
    args = parser.parse_args()
    if args.offline:
        return offline()
    if not args.kind or not args.target or args.wait < 1:
        parser.error("--live requires --kind, --target, and a positive --wait")
    try:
        return live(args.kind, args.target, args.wait)
    except LabOpsError as exc:
        print(_json({"error": exc.code, "message": str(exc), "detail": exc.detail}))
        return exc.exit


if __name__ == "__main__":
    raise SystemExit(main())
