import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labops.db import connect, migrate
from labops.runs import preview_progress_rules, scan_runs
from labops.tasks import set_repo_options


def _stamp(path: Path, value: str):
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    os.utime(path, (timestamp, timestamp))


def _record(state_root: Path, repo: Path, key: str, *, output: str, modified: str):
    state_dir = state_root / key
    state_dir.mkdir(parents=True)
    run_dir = repo.parent / "runs" / key[:8]
    run_dir.mkdir(parents=True)
    log_path = run_dir / "output.log"
    log_path.write_text(output, encoding="utf-8")
    _stamp(log_path, modified)
    (state_dir / "launch.json").write_text(json.dumps({
        "schemaVersion": 4, "runDirectory": str(run_dir), "scriptPath": str(repo / "train.py"),
        "workingDirectory": str(repo), "requestedAtUtc": "2026-10-21T10:00:00Z",
    }), encoding="utf-8")
    (state_dir / "process.json").write_text(json.dumps({
        "pid": 42, "processStartedAtUtc": "2026-10-21T10:00:00Z",
        "runDirectory": str(run_dir), "logPath": str(log_path),
    }), encoding="utf-8")
    status_path = state_dir / "status.json"
    status_path.write_text(json.dumps({
        "status": "running", "pid": 42, "startedAtUtc": "2026-10-21T10:00:00Z",
        "finishedAtUtc": None, "exitCode": None,
    }), encoding="utf-8")
    return status_path, log_path


def _set_rule(conn, rule):
    return set_repo_options(conn, "toy", progress_rule=[json.dumps(rule, ensure_ascii=False)])


def _hint(conn, key):
    row = conn.execute("SELECT progress_hint FROM bg_runs WHERE run_key=?", (key,)).fetchone()
    return row[0] if row else None


def main():
    checks = {"json_rule": False, "line_rule": False, "newest_wins": False,
              "finished_frozen": False, "injection_safe": False, "preview_ok": False}
    with tempfile.TemporaryDirectory(prefix="labops-smoke-progress-") as temporary:
        root = Path(temporary)
        repo = root / "repo"
        repo.mkdir()
        state = root / "background-state"
        state.mkdir()
        conn = connect(root / "home" / "labops.db")
        try:
            migrate(conn)
            conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy',?,?)",
                         (str(repo), "2026-10-21T10:00:00Z"))

            progress = repo / "metrics" / "stage_2" / "progress.json"
            progress.parent.mkdir(parents=True)
            progress.write_text(json.dumps({"phase": "eval", "done": 10, "total": 10}), encoding="utf-8")
            _stamp(progress, "2026-10-21T10:59:30Z")
            _status_a, log_a = _record(state, repo, "a" * 64, output="step 1/10\n",
                                       modified="2026-10-21T10:59:00Z")
            _set_rule(conn, {
                "name": "json", "glob": "metrics/**/progress.json", "type": "json",
                "path_regex": r"stage_(?P<stage>\d+)",
                "template": "stage {stage} · {phase} {done}/{total} · {missing}",
            })
            scan_runs(conn, state_root=state, now_ts="2026-10-21T11:00:00Z", process_info=lambda _pid: None)
            checks["json_rule"] = _hint(conn, "a" * 64) == "stage 2 · eval 10/10 · ?"
            preview = preview_progress_rules(conn, "toy")
            checks["preview_ok"] = (
                preview["progress_rule_errors"] == 0
                and preview["rules"][0]["file"] == "metrics/stage_2/progress.json"
                and preview["rules"][0]["fields"].get("stage") == "2"
                and preview["rules"][0]["progress_hint"] == _hint(conn, "a" * 64)
            )

            log_a.write_text("step 7/10\n", encoding="utf-8")
            _stamp(log_a, "2026-10-21T11:00:05Z")
            scan_runs(conn, state_root=state, now_ts="2026-10-21T11:00:10Z", process_info=lambda _pid: None)
            checks["newest_wins"] = _hint(conn, "a" * 64) == "step 7/10"

            line_file = repo / "logs" / "tqdm.log"
            line_file.parent.mkdir()
            line_file.write_bytes(b"step 2/9\rstep 8/9\rstep 9/9\r")
            _stamp(line_file, "2026-10-21T11:00:20Z")
            status_b, _log_b = _record(state, repo, "b" * 64, output="step 1/9\n",
                                       modified="2026-10-21T11:00:10Z")
            _set_rule(conn, {
                "name": "line", "glob": "logs/*.log", "type": "line_regex",
                "pattern": r"step (?P<step>\d+)/(?P<total>\d+)",
                "template": "step {step}/{total}",
            })
            scan_runs(conn, state_root=state, now_ts="2026-10-21T11:00:30Z", process_info=lambda _pid: None)
            checks["line_rule"] = _hint(conn, "b" * 64) == "step 9/9"

            line_file.write_bytes(b"step 2/9\rstep 9/9\rstep 9/9\r")
            _stamp(line_file, "2026-10-21T11:00:35Z")
            status = json.loads(status_b.read_text(encoding="utf-8"))
            status.update({"status": "completed", "finishedAtUtc": "2026-10-21T11:00:36Z", "exitCode": 0})
            status_b.write_text(json.dumps(status), encoding="utf-8")
            scan_runs(conn, state_root=state, now_ts="2026-10-21T11:00:40Z", process_info=lambda _pid: None)
            frozen_hint = _hint(conn, "b" * 64)
            line_file.write_bytes(b"step 10/10\r")
            _stamp(line_file, "2026-10-21T11:00:45Z")
            scan_runs(conn, state_root=state, now_ts="2026-10-21T11:00:50Z", process_info=lambda _pid: None)
            checks["finished_frozen"] = frozen_hint == "step 9/9" and _hint(conn, "b" * 64) == frozen_hint

            injection = repo / "metrics" / "injection.json"
            injection.write_text("{}", encoding="utf-8")
            _stamp(injection, "2026-10-21T11:00:55Z")
            _set_rule(conn, {
                "name": "injection", "glob": "metrics/injection.json", "type": "json",
                "template": "{__class__} {a.b}",
            })
            injection_preview = preview_progress_rules(conn, "toy")["rules"][0]
            checks["injection_safe"] = injection_preview["progress_hint"] == "? {a.b}"

            result = {"ok": all(checks.values()), **checks}
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            return 0 if result["ok"] else 1
        finally:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
