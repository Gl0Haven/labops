import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from labops.config import now
from labops.context import task_context
from labops.db import connect, migrate
from labops.evaluate import check_eval_file, evaluate_retrieval
from labops.notify import NtfySender, OpenClawSender, list_notifications, send_pending
from labops.progress import progress_summary
from labops.redact import for_actor
from labops.report import stats_report
from labops.search import search
from labops.tasks import add_repo, create_task, open_round
from tests.helpers import make_repo


class _Response:
    def getcode(self):
        return 204

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _pending(conn, event_key: str, text: str, created_at: str) -> None:
    conn.execute(
        "INSERT INTO notifications(event_key,kind,text,status,created_at) VALUES(?, 'verify_failed', ?, 'PENDING', ?)",
        (event_key, text, created_at),
    )


def main() -> int:
    checks = {
        "token_expired_paused": False,
        "probe_recovered": False,
        "ntfy_first_line_only": False,
        "ntfy_once_per_event": False,
        "alerts_paused_in_summary": False,
        "stats_ok": False,
        "eval_check_file_ok": False,
        "search_or_recall": False,
    }
    with tempfile.TemporaryDirectory(prefix="labops-reliability-") as temporary:
        root = Path(temporary)
        repo = root / "toy-repo"
        make_repo(repo)
        conn = connect(root / "home" / "labops.db")
        try:
            migrate(conn)
            add_repo(conn, repo_id="toy", path=repo)
            task = create_task(conn, repo_id="toy", task_id="T20261020-reliability-smoke",
                               goal="reliability smoke", profile={"schema": 1})
            round_data = open_round(conn, task_id=task["task_id"], actor="codex", role="implement")
            timestamp = now()
            first_line = f"[LabOps · toy] {task['task_id']} r{round_data['n']} · smoke"
            for index in range(1, 4):
                _pending(conn, f"ev:{index}", f"{first_line}\nprivate details\nnext step",
                         f"2026-10-20T10:0{index}:00Z")

            with patch.dict(os.environ, {"LABOPS_NOTIFY_ACCOUNT": "test", "LABOPS_NOTIFY_TARGET": "test"}), \
                 patch("labops.notify.shutil.which", return_value="openclaw.CMD"), \
                 patch("labops.notify.subprocess.run") as openclaw_run:
                openclaw_run.return_value.stdout = ""
                openclaw_run.return_value.stderr = "ret=-2 prepare failed"
                openclaw_run.return_value.returncode = 1
                expired_result = OpenClawSender()("probe")

            ntfy_requests = []

            def fake_urlopen(request, *, timeout, context):
                ntfy_requests.append({"request": request, "timeout": timeout, "context": context})
                return _Response()

            ntfy_sender = NtfySender("reliability-smoke")
            with patch("labops.notify._ssl_contexts", return_value=[object()]), \
                 patch("labops.notify.urllib.request.urlopen", side_effect=fake_urlopen):
                first = send_pending(conn, lambda _text: ("token_expired", "openclaw_token_expired"), limit=10,
                                     ntfy_sender=ntfy_sender, ntfy_topic="reliability-smoke")
                first_row = conn.execute("SELECT status,attempts FROM notifications WHERE event_key='ev:1'").fetchone()
                paused_row = conn.execute("SELECT value FROM meta WHERE key='wechat_paused_at'").fetchone()
                paused_summary = for_actor(conn, "secretary", progress_summary(conn))
                paused_context = for_actor(conn, "secretary", task_context(conn, task_id=task["task_id"]))
                failed_probe = send_pending(conn, lambda _text: ("failed", "prepare failed"), limit=10,
                                            ntfy_sender=ntfy_sender, ntfy_topic="reliability-smoke")
                recovered = send_pending(conn, lambda _text: "sent", limit=10,
                                         ntfy_sender=ntfy_sender, ntfy_topic="reliability-smoke")

            rows = list_notifications(conn)
            first_request = ntfy_requests[0]["request"] if ntfy_requests else None
            checks["token_expired_paused"] = (
                expired_result == ("token_expired", "openclaw_token_expired")
                and first["token_expired"] == 1
                and first_row["attempts"] == 0
                and first_row["status"] == "PENDING"
                and paused_row is not None
                and failed_probe["failed"] == 1
            )
            checks["probe_recovered"] = (
                recovered["accepted"] == 3
                and conn.execute("SELECT 1 FROM meta WHERE key='wechat_paused_at'").fetchone() is None
            )
            checks["ntfy_first_line_only"] = (
                first_request is not None
                and first_request.data == first_line.encode("utf-8")
                and first_request.get_header("Title") == "LabOps"
                and first_request.get_header("Tags") == "warning"
            )
            checks["ntfy_once_per_event"] = (
                len(ntfy_requests) == 1 and rows[0]["ntfy_status"] == "ntfy_sent"
            )
            expected_pause = {"since": paused_summary.get("alerts_paused", {}).get("since"),
                              "hint": "微信凭证失效：请给 bot 发一句话"}
            checks["alerts_paused_in_summary"] = (
                paused_summary.get("alerts_paused") == expected_pause
                and paused_context.get("alerts_paused") == expected_pause
            )

            report = stats_report(conn)
            checks["stats_ok"] = (
                report["tasks"]["total"] == 1
                and report["tasks"]["rounds_by_task"][0]["rounds"] == 1
                and report["alerts"]["by_status"] == {"ACCEPTED": 3}
                and report["repositories"]["registered_count"] == 1
            )

            ref = "D-20261020-1"
            conn.execute(
                "INSERT INTO search_docs(doc_id,repo_id,kind,ref,path,line,title,body,ts,content_hash,superseded_by,confidential,indexed_at) "
                "VALUES('doc-smoke','toy','decision',?,NULL,NULL,?,'smoke body',NULL,'hash',NULL,0,?)",
                (ref, ref, timestamp),
            )
            questions = root / "questions.jsonl"
            questions.write_text(json.dumps({"id": "q1", "query": ref,
                                            "expected": [{"repo_id": "toy", "ref": ref}]}) + "\n",
                                 encoding="utf-8")
            checked = check_eval_file(conn, file=questions)
            evaluated = evaluate_retrieval(conn, file=questions, ks=[1], out=root / "eval", repo_filter="toy")
            checks["eval_check_file_ok"] = (
                checked["ok"] and checked["expected"] == 1
                and evaluated["report"]["questions"] == 1
                and evaluated["report"]["metrics"]["recall@1"] == 1.0
            )
            conn.execute(
                "INSERT INTO search_docs(doc_id,repo_id,kind,ref,path,line,title,body,ts,content_hash,superseded_by,confidential,indexed_at) "
                "VALUES('doc-partial','toy','status','SMK-1',NULL,NULL,'alpha','beta result',NULL,'hash',NULL,0,?)",
                (timestamp,),
            )
            conn.execute("INSERT INTO search_tri(doc_id,text) VALUES('doc-partial','alpha beta result')")
            conn.execute("INSERT INTO search_bi(doc_id,text) VALUES('doc-partial','alpha beta result')")
            partial = search(conn, "alpha missingtoken", repo_id="toy")
            checks["search_or_recall"] = any(item["ref"] == "SMK-1" for item in partial["results"])
        finally:
            conn.close()

    result = {"ok": all(checks.values()), **checks}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
