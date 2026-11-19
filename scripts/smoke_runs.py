import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labops.db import connect, migrate
from labops.notify import merge_pending, notify_config, scan, send_pending
from labops.rewrite import rewrite
from labops.runs import scan_runs


def _record(state_root: Path, repo: Path, key: str, status: str, *, error_code=None):
    state_dir = state_root / key
    run_dir = repo / "runs" / f"run-{key[:6]}"
    state_dir.mkdir(parents=True)
    run_dir.mkdir(parents=True)
    (state_dir / "launch.json").write_text(json.dumps({
        "schemaVersion": 4, "runDirectory": str(run_dir), "scriptPath": str(repo / "train.py"),
        "workingDirectory": str(repo), "requestedAtUtc": "2026-10-20T11:00:00Z",
    }), encoding="utf-8")
    (state_dir / "process.json").write_text(json.dumps({
        "pid": 42, "processStartedAtUtc": "2026-10-20T11:00:00Z",
        "runDirectory": str(run_dir), "logPath": str(run_dir / "output.log"),
    }), encoding="utf-8")
    (state_dir / "status.json").write_text(json.dumps({
        "status": status, "pid": 42, "startedAtUtc": "2026-10-20T11:00:00Z",
        "finishedAtUtc": "2026-10-20T11:30:00Z" if status != "running" else None,
        "exitCode": error_code,
    }), encoding="utf-8")
    (run_dir / "output.log").write_text("step 1\n", encoding="utf-8")
    return state_dir, run_dir


def main():
    with tempfile.TemporaryDirectory(prefix="labops-smoke-runs-") as td:
        root = Path(td)
        repo = root / "repo"
        repo.mkdir()
        state_root = root / "background-state"
        state_root.mkdir()
        conn = connect(root / "home" / "labops.db")
        try:
            migrate(conn)
            conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy',?,?)",
                         (str(repo), "2026-10-20T10:00:00Z"))
            os.environ["LABOPS_BG_STATE_ROOT"] = str(state_root)
            os.environ["LABOPS_NTFY_TOPIC"] = ""
            notify_config(conn, digest_minutes=0)

            _record(state_root, repo, "a" * 64, "completed")
            history = scan_runs(conn, now_ts="2026-10-20T12:00:00Z")
            history_silent = history["events"] == 0 and conn.execute(
                "SELECT COUNT(*) FROM events WHERE entity_type='bg_run'"
            ).fetchone()[0] == 0

            failed_dir, _ = _record(state_root, repo, "b" * 64, "running")
            scan_runs(conn, now_ts="2026-10-20T12:01:00Z")
            status_file = failed_dir / "status.json"
            status = json.loads(status_file.read_text(encoding="utf-8"))
            status.update({"status": "failed", "finishedAtUtc": "2026-10-20T12:02:00Z", "exitCode": 2})
            status_file.write_text(json.dumps(status), encoding="utf-8")
            scan_runs(conn, now_ts="2026-10-20T12:02:00Z")
            scan(conn)
            sent = []
            failure_counts = send_pending(conn, lambda text: sent.append(text) or "sent")
            failed_urgent = failure_counts["accepted"] == 1 and len(sent) == 1 and "· 失败" in sent[0]

            stalled_dir, stalled_run = _record(state_root, repo, "c" * 64, "running")
            fresh_mtime = datetime.fromisoformat("2026-10-20T11:59:00+00:00").timestamp()
            os.utime(stalled_run / "output.log", (fresh_mtime, fresh_mtime))
            conn.execute("INSERT INTO meta(key,value) VALUES('runs_stall_minutes','1')")
            started = scan_runs(conn, now_ts="2026-10-20T12:00:00Z")
            old_mtime = datetime.fromisoformat("2026-10-20T11:58:00+00:00").timestamp()
            os.utime(stalled_run / "output.log", (old_mtime, old_mtime))
            first_stall = scan_runs(conn, now_ts="2026-10-20T12:00:01Z")
            second_stall = scan_runs(conn, now_ts="2026-10-20T12:00:30Z")
            stall_once = (started["events"] == 1 and first_stall["events"] == 1
                          and second_stall["events"] == 0 and conn.execute(
                "SELECT COUNT(*) FROM events WHERE entity_id=? AND to_state='run_stalled'", ("c" * 64,)
            ).fetchone()[0] == 1)

            conn.execute(
                "INSERT INTO bg_runs(run_key,repo_id,run_dir,status,progress_hint,progress_at,log_mtime,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("d" * 64, "toy", str(repo / "runs" / "run-progress"), "running", "epoch 2/8",
                 "2026-10-20T11:59:00Z", "2026-10-20T11:59:00Z", "2026-10-20T12:00:00Z"),
            )
            conn.execute("INSERT INTO notifications(event_key,kind,text,status,created_at) "
                         "VALUES('ev:digest','submitted','[LabOps · toy] task · 已提交','PENDING','2026-10-20T10:00:00Z')")
            merge_pending(conn, now_ts="2026-10-20T12:00:00Z")
            digest = conn.execute("SELECT text FROM notifications WHERE kind='batch_digest'").fetchone()[0]
            digest_has_progress = "训练进度" in digest and "epoch 2/8" in digest

            template = "[LabOps · toy] 训练 trial-a · T20261020-labops-runs r1 · 用时 12 分钟\n下一步：调用 labops runs list\n任务：训练进度接入"
            with patch("labops.rewrite._request_completion", return_value=template):
                rewritten, outcome = rewrite(template, kind="batch_digest", confidential=False,
                                              enabled=True, api_key_available=True)
            rewrite_llm = outcome == "llm" and rewritten == template
            with patch("labops.rewrite._request_completion", return_value=template.replace("T20261020-labops-runs", "missing")):
                fallback, fallback_outcome = rewrite(template, kind="batch_digest", confidential=False,
                                                     enabled=True, api_key_available=True)
            rewrite_fallback_on_missing_id = fallback_outcome == "template_check_failed" and fallback == template
            no_key, no_key_outcome = rewrite(template, kind="batch_digest", confidential=False,
                                             enabled=True, api_key_available=False)
            no_key_template = no_key_outcome == "template_disabled" and no_key == template

            checks = {"history_silent": history_silent, "failed_urgent": failed_urgent,
                      "stall_once": stall_once, "digest_has_progress": digest_has_progress,
                      "rewrite_llm": rewrite_llm,
                      "rewrite_fallback_on_missing_id": rewrite_fallback_on_missing_id,
                      "no_key_template": no_key_template}
            result = {"ok": all(checks.values()), "runs_tracked": conn.execute(
                "SELECT COUNT(*) FROM bg_runs"
            ).fetchone()[0], **checks}
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            return 0 if result["ok"] else 1
        finally:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
