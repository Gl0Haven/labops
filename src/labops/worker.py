from . import reconcile as reconcile_module
from .spool import drain
from .verify import verify_round


def run_once(conn) -> dict:
    spool_result = drain(conn)
    repo_rows = conn.execute("SELECT repo_id FROM repos ORDER BY repo_id").fetchall()
    reconciled = []
    for row in repo_rows:
        repo_id = row["repo_id"]
        reconciled.append({"repo_id": repo_id, "result": reconcile_module.reconcile(conn, repo_id)})
    rounds = conn.execute(
        "SELECT r.round_id,t.repo_id FROM rounds r JOIN tasks t ON t.task_id=r.task_id "
        "JOIN repos p ON p.repo_id=t.repo_id "
        "WHERE p.trusted_for_exec=1 AND r.state='CLAIMED' ORDER BY r.created_at,r.round_id"
    ).fetchall()
    verified = []
    for row in rounds:
        try:
            verified.append({"round_id": row["round_id"], "result": verify_round(conn, row["round_id"])})
        except Exception as exc:
            verified.append({"round_id": row["round_id"],
                             "error": {"type": type(exc).__name__, "message": str(exc)}})
    return {"spool": spool_result, "reconciled": reconciled, "verified": verified}
