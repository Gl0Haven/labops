import multiprocessing
from pathlib import Path

from labops.db import connect, migrate
from labops.errors import ValidationError
from labops.gpuq import queue_choose, queue_tick


def _race_worker(db_path, queue_ids, mode, worker_index, iterations, barrier, results):
    conn = connect(db_path)
    try:
        for iteration in range(iterations):
            barrier.wait(timeout=60)
            try:
                if mode == "choose_choose" or worker_index == 1:
                    queue_choose(conn, queue_id=queue_ids[worker_index], choice="now",
                                 actor="secretary", pid_alive=lambda _pid: False,
                                 now_ts="2026-10-21T10:00:00Z")
                    result = {"kind": "ok"}
                else:
                    result = {"kind": "ok", **queue_tick(
                        conn, now_ts="2026-10-21T10:00:00Z", pid_alive=lambda _pid: False,
                    )}
            except ValidationError as exc:
                result = {"kind": "validation", "message": str(exc)}
            except BaseException as exc:  # send worker faults to the parent instead of hanging the barrier
                result = {"kind": "unexpected", "message": f"{type(exc).__name__}: {exc}"}
            results.put((iteration, worker_index, result))
            barrier.wait(timeout=60)
    finally:
        conn.close()


def run_race_scenario(root: str | Path, mode: str, *, iterations: int = 20) -> dict:
    if mode not in {"choose_choose", "tick_choose"}:
        raise ValueError("invalid race mode")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    repo = root / "repo"
    repo.mkdir(exist_ok=True)
    entrypoint = repo / "train.ps1"
    entrypoint.write_text("Write-Output 'race fixture'\n", encoding="utf-8")
    db_path = root / "labops.db"
    conn = connect(db_path)
    migrate(conn)
    conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy',?,?)",
                 (str(repo), "2026-10-21T09:00:00Z"))
    queue_ids = ("race-a", "race-b")
    for index, queue_id in enumerate(queue_ids):
        conn.execute(
            "INSERT INTO gpu_queue(queue_id,repo_id,title,note,entrypoint,entrypoint_sha256,working_dir,"
            "run_prefix,context,created_by,status,mode,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'codex','ASKED',NULL,?,?)",
            (queue_id, "toy", queue_id, "", str(entrypoint), "unused", str(repo),
             f"race-{index}", "temporary race fixture", "2026-10-21T09:00:00Z",
             "2026-10-21T09:00:00Z"),
        )
    conn.execute("INSERT INTO meta(key,value) VALUES('queue_launch_mode','handoff')")
    conn.close()

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    results = context.Queue()
    workers = [context.Process(
        target=_race_worker,
        args=(str(db_path), queue_ids, mode, index, iterations, barrier, results),
    ) for index in range(2)]
    for worker in workers:
        worker.start()

    checks = []
    parent = connect(db_path)
    try:
        for iteration in range(iterations):
            with parent:
                parent.execute(
                    "UPDATE gpu_queue SET status='ASKED',mode=NULL,notified_idle_at=NULL,"
                    "launch_attempted_at=NULL,launched_run_key=NULL,launch_error=NULL,"
                    "launch_fingerprint=NULL,updated_at=? WHERE queue_id IN (?,?)",
                    ("2026-10-21T09:00:00Z", *queue_ids),
                )
                if mode == "tick_choose":
                    parent.execute("UPDATE gpu_queue SET status='WAIT_AUTO',mode='auto' WHERE queue_id=?",
                                   (queue_ids[0],))
            barrier.wait(timeout=60)
            barrier.wait(timeout=60)
            outcomes = {}
            for _ in range(2):
                result_iteration, worker_index, result = results.get(timeout=10)
                if result_iteration != iteration:
                    checks.append(False)
                outcomes[worker_index] = result
            launching = parent.execute(
                "SELECT COUNT(*) FROM gpu_queue WHERE status='LAUNCHING'"
            ).fetchone()[0]
            statuses = {row["queue_id"]: row["status"] for row in parent.execute(
                "SELECT queue_id,status FROM gpu_queue WHERE queue_id IN (?,?)", queue_ids
            )}
            if mode == "choose_choose":
                kinds = sorted(result["kind"] for result in outcomes.values())
                errors = [result.get("message") for result in outcomes.values()
                          if result["kind"] == "validation"]
                checks.append(kinds == ["ok", "validation"] and len(errors) == 1
                              and errors[0] in {"queue_launch_in_progress", "gpu_state_changed"}
                              and launching == 1)
            else:
                unexpected = [result for result in outcomes.values()
                              if result["kind"] == "unexpected"
                              or (result["kind"] == "validation"
                                  and result.get("message") not in {"queue_launch_in_progress", "gpu_state_changed"})]
                checks.append(not unexpected and launching == 1
                              and sum(status == "LAUNCHING" for status in statuses.values()) == 1)
    finally:
        parent.close()
        for worker in workers:
            worker.join(timeout=60)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        results.close()
        results.join_thread()
    exit_codes = [worker.exitcode for worker in workers]
    return {"ok": bool(checks) and all(checks) and exit_codes == [0, 0],
            "iterations": iterations, "checks": checks, "worker_exit_codes": exit_codes}
