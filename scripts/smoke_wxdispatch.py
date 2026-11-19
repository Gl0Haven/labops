import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from labops import desktop, desktop_codex, notify
from tests.test_desktop import (HOST_ID, THREAD_ID, TOKEN, ClaudeSink, FakeCodexStream, append_jsonl,
                                desktop_fixture, rollout_event, transcript_record)


def offline() -> int:
    with desktop_fixture() as fixture:
        conn = fixture.conn
        sink = ClaudeSink()
        stream = FakeCodexStream()
        claude = desktop.desktop_dispatch(
            conn, kind="claude", target=HOST_ID, prompt="请回报 fixture 结果", request_id="smoke-claude",
            actor="secretary", process_info=fixture.process_info, claude_open=lambda _: sink,
        )
        codex = desktop.desktop_dispatch(
            conn, kind="codex", target=THREAD_ID, prompt="请回报 fixture 结果", request_id="smoke-codex",
            actor="secretary", codex_connect=lambda _: desktop_codex.IpcClient(stream),
        )
        claude_prompt = json.loads(sink.payload.splitlines()[1])["message"]["content"]
        append_jsonl(fixture.transcript, transcript_record("user", claude_prompt),
                     transcript_record("assistant", "Claude 离线验收通过"))
        append_jsonl(fixture.rollout, rollout_event("task_started"),
                     rollout_event("task_complete", last_agent_message="Codex 离线验收通过"))

        messages = []

        def sender(text):
            messages.append(text)
            return "sent"

        with patch("labops.desktop.runs._process_info", fixture.process_info):
            first = notify.run(conn, sender=sender, ntfy_topic="")
            report = notify.notify_user(conn, actor="codex", text="主动回报离线验收通过", request_id="smoke-report")
            second = notify.run(conn, sender=sender, ntfy_topic="")

        relay_sink = ClaudeSink()
        relay_stream = FakeCodexStream()
        to_claude = desktop.message_claude(
            conn, target=HOST_ID, prompt="T20261119-labops-wxdispatch Q1 请用户确认 fixture 结论。",
            request_id="smoke-to-claude", actor="codex", process_info=fixture.process_info,
            claude_open=lambda _: relay_sink,
        )
        to_codex = fixture.send("codex", target=THREAD_ID, request_id="smoke-to-codex",
                                prompt="已收到 fixture 回报。", codex_connect=lambda _: desktop_codex.IpcClient(relay_stream))
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}):
            third = notify.run(conn, sender=sender, ntfy_topic="")

        rows = conn.execute(
            "SELECT event_key,kind,text,status,rewritten_text,rewrite_outcome FROM notifications ORDER BY event_key"
        ).fetchall()
        relays = {row["event_key"]: row["rewritten_text"] for row in rows
                  if row["kind"] == "agent_relay" and row["status"] == "ACCEPTED"
                  and row["rewrite_outcome"] == "template_disabled"}
        relay_ok = (to_claude["state"] == to_codex["state"] == "SENT" and third["accepted"] == len(relays) == 2
                    and relays.get("relay:smoke-to-claude", "").startswith("[LabOps · Codex → Claude]")
                    and relays.get("relay:smoke-to-codex", "").startswith("[LabOps · Claude → Codex]")
                    and all(text in messages for text in relays.values()))
        replies = {row["event_key"]: row["text"] for row in rows
                   if row["kind"] == "desktop_reply" and row["status"] == "ACCEPTED"}
        reports = [row["text"] for row in rows if row["kind"] == "agent_report" and row["status"] == "ACCEPTED"]
        report_sent = (report["queued"] and second["accepted"] == 1
                       and reports == ["[LabOps · Codex 回报]\n主动回报离线验收通过"])
        no_token = TOKEN not in json.dumps(
            [claude, codex, to_claude, to_codex, first, report, second, third, messages], ensure_ascii=False,
        )
        reply_ok = (first["desktop_reported"] == first["accepted"] == 2 and len(replies) == 2
                    and "Claude 离线验收通过" in replies.get("desktop:smoke-claude", "")
                    and "Codex 离线验收通过" in replies.get("desktop:smoke-codex", ""))
        ok = claude["state"] == codex["state"] == "SENT" and reply_ok and report_sent and relay_ok and no_token
        for kind, message in (("desktop_reply", replies.get("desktop:smoke-claude")),
                              ("agent_report", reports[0] if reports else None),
                              ("agent_relay", relays.get("relay:smoke-to-claude"))):
            if message and TOKEN not in message:
                print(json.dumps({kind: message}, ensure_ascii=False, separators=(",", ":")))
        print(json.dumps({"ok": ok, "dispatch_claude": claude["state"], "dispatch_codex": codex["state"],
                          "replies_sent": len(replies), "agent_report_sent": report_sent, "relays_sent": len(relays),
                          "no_token_in_output": no_token}, separators=(",", ":")))
        return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", required=True)
    parser.parse_args()
    return offline()


if __name__ == "__main__":
    raise SystemExit(main())
