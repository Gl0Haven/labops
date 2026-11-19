import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from labops.db import connect, migrate
from labops.gpuq import (_fingerprint, _fingerprint_changes, queue_choose,
                         queue_register, queue_tick)
from tests.gpuq_harden_support import run_race_scenario
from tests.helpers import commit_file, make_repo


def main():
    checks = {"race_choose_choose": False, "race_tick_choose": False, "unique_index": False,
              "fingerprint_dirty": False, "fingerprint_config": False,
              "launch_not_blocked_by_fingerprint": False}
    with tempfile.TemporaryDirectory(prefix="labops-smoke-gpuq-harden-") as temporary:
        root = Path(temporary)
        repo = root / "toy-repo"
        make_repo(repo, files={
            "scripts/train.ps1": "Write-Output 'smoke'\n",
            "config.yaml": "batch_size: 8\n",
            "train.py": "print('toy')\n",
        })
        conn = connect(root / "home" / "labops.db")
        try:
            migrate(conn)
            conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy',?,?)",
                         (str(repo), "2026-10-21T09:00:00Z"))
            entrypoint = repo / "scripts" / "train.ps1"
            with patch("labops.gpuq._ALLOWED_ENTRY_ROOTS", (root,)):
                queue = queue_register(
                    conn, repo_id="toy", title="fingerprint smoke", entrypoint=str(entrypoint),
                    context="temporary fixture", config_paths=["config.yaml"], created_by="cli",
                )["queue_item"]
            baseline = json.loads(queue["register_fingerprint"])
            commit_file(repo, "train.py", "print('committed after registration')\n", "advance toy HEAD")
            (repo / "config.yaml").write_text("batch_size: 32\n", encoding="utf-8")
            (repo / "untracked.bin").write_bytes(b"count only")
            current = _fingerprint(repo, ["config.yaml"])
            changed = _fingerprint_changes(baseline, current)
            checks["fingerprint_dirty"] = current["git_dirty"] is True and current["untracked_count"] == 1
            checks["fingerprint_config"] = (
                current["configs"][0]["sha256"] != baseline["configs"][0]["sha256"]
                and "config.yaml" in changed
            )

            index = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_gpu_queue_one_launching'"
            ).fetchone()
            checks["unique_index"] = index is not None
            conn.execute("INSERT INTO meta(key,value) VALUES('queue_launch_mode','launcher') "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            queue_tick(conn, "2026-10-21T10:00:00Z", pid_alive=lambda _pid: False)
            queue_choose(conn, queue_id=queue["queue_id"], choice="now", actor="secretary",
                         pid_alive=lambda _pid: False, now_ts="2026-10-21T10:01:00Z")
            launched = queue_tick(
                conn, "2026-10-21T10:02:00Z", pid_alive=lambda _pid: False,
                launcher=lambda _row, _run_dir: {"ok": True},
            )
            queue_status = conn.execute("SELECT status FROM gpu_queue WHERE queue_id=?",
                                         (queue["queue_id"],)).fetchone()[0]
            checks["launch_not_blocked_by_fingerprint"] = (
                launched["launching"] == 1 and launched["launch_failed"] == 0
                and queue_status == "LAUNCHING"
            )
        finally:
            conn.close()

        checks["race_choose_choose"] = run_race_scenario(
            root / "race-choose", "choose_choose", iterations=20
        )["ok"]
        checks["race_tick_choose"] = run_race_scenario(
            root / "race-tick", "tick_choose", iterations=20
        )["ok"]
        result = {"ok": all(checks.values()), **checks}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
