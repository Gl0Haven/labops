import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import home, now
from .canon import canonical_json
from .errors import ValidationError

SCHEMA_VERSION = 16
DEFAULT_HANDOFF_RULES = (
    "- LabOps 的状态变更和 git 操作只走 MCP（start_work / snapshot / submit_changes / submit_review）；读写文件、改代码、跑测试用你自己的工具。\n"
    "- 收尾由你通过 MCP 完成，不等任何人；提交后不再修改报告。\n"
    "- 不读写其他真实项目，不 git commit，不联网，不装包；遇到计划中的停止条件就停下并写 open-questions。"
)

_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS repos(
      repo_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
      main_ref TEXT NOT NULL DEFAULT 'refs/heads/main', worktree_root TEXT,
      trusted_for_exec INTEGER NOT NULL DEFAULT 0, confidential INTEGER NOT NULL DEFAULT 0,
      policy_json TEXT, policy_hash TEXT, run_watch_globs TEXT, run_progress_rules TEXT,
      created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS acceptance_profiles(
      profile_hash TEXT PRIMARY KEY, content_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS tasks(
      task_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repos,
      goal TEXT NOT NULL, state TEXT NOT NULL, base_commit TEXT NOT NULL,
      profile_hash TEXT NOT NULL REFERENCES acceptance_profiles, policy_hash TEXT, policy_json TEXT,
      source TEXT NOT NULL DEFAULT 'native', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS rounds(
      round_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks, n INTEGER NOT NULL,
      actor TEXT NOT NULL, role TEXT NOT NULL, base_commit TEXT NOT NULL, state TEXT NOT NULL,
      head_commit TEXT, head_tree TEXT, branch TEXT, worktree_path TEXT, claim_id TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(task_id, n))""",
    """CREATE TABLE IF NOT EXISTS claims(
      claim_id TEXT PRIMARY KEY, round_id TEXT NOT NULL REFERENCES rounds, actor TEXT NOT NULL,
      kind TEXT NOT NULL, client_request_id TEXT NOT NULL, payload_json TEXT NOT NULL,
      payload_hash TEXT NOT NULL, fingerprint TEXT, status TEXT NOT NULL, duplicate_of TEXT,
      reject_reason TEXT, created_at TEXT NOT NULL, source TEXT,
      UNIQUE(round_id, actor, kind, client_request_id))""",
    """CREATE TABLE IF NOT EXISTS evidence(
      evidence_id TEXT PRIMARY KEY, round_id TEXT NOT NULL REFERENCES rounds, kind TEXT NOT NULL,
      status TEXT NOT NULL, commit_oid TEXT NOT NULL, tree_oid TEXT NOT NULL,
      profile_hash TEXT NOT NULL, results_json TEXT, env_json TEXT, producer TEXT NOT NULL,
      created_at TEXT NOT NULL, finished_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS reviews(
      review_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks,
      round_id TEXT NOT NULL REFERENCES rounds, reviewer TEXT NOT NULL, state TEXT NOT NULL,
      verdict TEXT, independence TEXT, base_commit TEXT NOT NULL, reviewed_commit TEXT NOT NULL,
      reviewed_tree TEXT NOT NULL, scope_id TEXT NOT NULL, acceptance_profile_hash TEXT NOT NULL,
      findings_json TEXT, claim_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS approvals(
      approval_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks, kind TEXT NOT NULL,
      target_commit TEXT NOT NULL, target_tree TEXT NOT NULL, decision_id TEXT,
      integration_id TEXT, granted_via TEXT NOT NULL, created_at TEXT NOT NULL, consumed_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS integrations(
      integration_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks,
      candidate_commit TEXT NOT NULL, candidate_tree TEXT NOT NULL, main_before TEXT NOT NULL,
      state TEXT NOT NULL, check_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS operations(
      op_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repos, kind TEXT NOT NULL,
      ref TEXT, expected_old TEXT, target TEXT, status TEXT NOT NULL,
      idempotency_id TEXT NOT NULL UNIQUE, on_applied_json TEXT, detail_json TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS events(
      seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, entity_type TEXT NOT NULL,
      entity_id TEXT NOT NULL, from_state TEXT, to_state TEXT, actor TEXT, detail_json TEXT)""",
    """CREATE TABLE IF NOT EXISTS spool_events(
      event_id TEXT PRIMARY KEY, client TEXT NOT NULL, event_type TEXT NOT NULL,
      session_id TEXT, turn_id TEXT, cwd TEXT, ts TEXT NOT NULL, payload_hash TEXT NOT NULL,
      ingested_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS notifications(
      event_key TEXT PRIMARY KEY, task_id TEXT, kind TEXT NOT NULL, text TEXT NOT NULL,
      status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
      created_at TEXT NOT NULL, sent_at TEXT, ntfy_status TEXT,
      rewritten_text TEXT, rewrite_outcome TEXT)""",
    """CREATE TABLE IF NOT EXISTS bg_runs(
      run_key TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repos,
      run_dir TEXT NOT NULL, script_path TEXT, status TEXT NOT NULL,
      pid INTEGER, started_at TEXT, finished_at TEXT, exit_code INTEGER,
      progress_hint TEXT, progress_at TEXT, log_mtime TEXT,
      last_notified_status TEXT, stall_notified INTEGER NOT NULL DEFAULT 0,
      orphaned INTEGER NOT NULL DEFAULT 0,
      source TEXT NOT NULL DEFAULT 'background_run', process_started_at TEXT,
      command_line TEXT, title TEXT, log_path TEXT, watch_globs TEXT,
      updated_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_rounds_task ON rounds(task_id, n)",
    "CREATE INDEX IF NOT EXISTS ix_reviews_round ON reviews(round_id)",
    "CREATE INDEX IF NOT EXISTS ix_events_entity ON events(entity_type, entity_id)",
]


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path is not None else home() / "labops.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _upgrade_to_v2(conn: sqlite3.Connection) -> None:
    task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "policy_json" not in task_columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN policy_json TEXT")
        for task in conn.execute("SELECT task_id FROM tasks ORDER BY task_id").fetchall():
            event = conn.execute(
                "SELECT detail_json FROM events WHERE entity_type='task' AND entity_id=? "
                "AND to_state='CREATED' ORDER BY seq LIMIT 1",
                (task["task_id"],),
            ).fetchone()
            if event is None:
                continue
            try:
                detail = json.loads(event[0] or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValidationError("invalid_created_event_json", detail={"task_id": task["task_id"]}) from exc
            frozen_policy = detail.get("frozen_policy") if isinstance(detail, dict) else None
            policy_json = canonical_json(frozen_policy).decode("utf-8") if frozen_policy is not None else None
            conn.execute("UPDATE tasks SET policy_json=? WHERE task_id=?", (policy_json, task["task_id"]))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS spool_events(
          event_id TEXT PRIMARY KEY, client TEXT NOT NULL, event_type TEXT NOT NULL,
          session_id TEXT, turn_id TEXT, cwd TEXT, ts TEXT NOT NULL, payload_hash TEXT NOT NULL,
          ingested_at TEXT NOT NULL)"""
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', '2') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _upgrade_to_v3(conn: sqlite3.Connection) -> None:
    repo_columns = {row[1] for row in conn.execute("PRAGMA table_info(repos)")}
    if "confidential" not in repo_columns:
        conn.execute("ALTER TABLE repos ADD COLUMN confidential INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS notifications(
          event_key TEXT PRIMARY KEY, task_id TEXT, kind TEXT NOT NULL, text TEXT NOT NULL,
          status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
          created_at TEXT NOT NULL, sent_at TEXT)"""
    )
    cursor = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('notify_cursor', ?) "
        "ON CONFLICT(key) DO NOTHING",
        (str(cursor),),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', '3') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _upgrade_to_v4(conn: sqlite3.Connection) -> None:
    repo_columns = {row[1] for row in conn.execute("PRAGMA table_info(repos)")}
    for name, declaration in (
        ("python", "TEXT"),
        ("backup_root", "TEXT"),
        ("backup_keep", "INTEGER NOT NULL DEFAULT 7"),
        ("legacy_bridge", "TEXT"),
    ):
        if name not in repo_columns:
            conn.execute(f"ALTER TABLE repos ADD COLUMN {name} {declaration}")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS backups(
          backup_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repos, path TEXT NOT NULL,
          head TEXT, ref_count INTEGER, sha256 TEXT NOT NULL, verified INTEGER NOT NULL,
          created_at TEXT NOT NULL)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS legacy_records(
          repo_id TEXT NOT NULL REFERENCES repos, kind TEXT NOT NULL, key TEXT NOT NULL,
          path TEXT NOT NULL, line INTEGER, title TEXT, value TEXT, content_hash TEXT NOT NULL,
          imported_at TEXT NOT NULL, PRIMARY KEY(repo_id, kind, key))"""
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', '4') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _create_legacy_records_v5(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS legacy_records(
          repo_id TEXT NOT NULL REFERENCES repos, kind TEXT NOT NULL, key TEXT NOT NULL, ref TEXT,
          path TEXT NOT NULL, line INTEGER, title TEXT, value TEXT, content_hash TEXT NOT NULL,
          imported_at TEXT NOT NULL, PRIMARY KEY(repo_id, kind, key))"""
    )


def _upgrade_to_v5(conn: sqlite3.Connection) -> None:
    legacy_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_records'"
    ).fetchone()
    if legacy_exists:
        conn.execute("ALTER TABLE legacy_records RENAME TO legacy_records_v4")
        _create_legacy_records_v5(conn)
        conn.execute("DROP TABLE legacy_records_v4")
    else:
        _create_legacy_records_v5(conn)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS search_docs(
          doc_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repos, kind TEXT NOT NULL,
          ref TEXT NOT NULL, path TEXT, line INTEGER, title TEXT, body TEXT NOT NULL, ts TEXT,
          content_hash TEXT NOT NULL, superseded_by TEXT, confidential INTEGER NOT NULL,
          indexed_at TEXT NOT NULL)"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_search_docs_repo_kind ON search_docs(repo_id,kind)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_search_docs_ref ON search_docs(ref)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_tri USING fts5(doc_id UNINDEXED,text,tokenize='trigram')")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_bi USING fts5(doc_id UNINDEXED,text,tokenize='unicode61')")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', '5') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _upgrade_to_v6(conn: sqlite3.Connection) -> None:
    tasks_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone()
    task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")} if tasks_exists else set()
    if tasks_exists and "brief" not in task_columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN brief TEXT")
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('handoff_rules',?) ON CONFLICT(key) DO NOTHING",
        (DEFAULT_HANDOFF_RULES,),
    )
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version','6') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _upgrade_to_v7(conn: sqlite3.Connection) -> None:
    repos_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repos'"
    ).fetchone()
    repo_columns = {row[1] for row in conn.execute("PRAGMA table_info(repos)")} if repos_exists else set()
    if repos_exists and "alert_detail" not in repo_columns:
        conn.execute("ALTER TABLE repos ADD COLUMN alert_detail INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version','7') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _upgrade_to_v8(conn: sqlite3.Connection) -> None:
    notifications_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notifications'"
    ).fetchone()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(notifications)")} if notifications_exists else set()
    if notifications_exists and "ntfy_status" not in columns:
        conn.execute("ALTER TABLE notifications ADD COLUMN ntfy_status TEXT")
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version','8') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _upgrade_to_v9(conn: sqlite3.Connection) -> None:
    # Claims record whether they arrived through MCP or the CLI; rows written before v9 stay NULL
    # (unknown) rather than being guessed.
    claims_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'").fetchone()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(claims)")} if claims_exists else set()
    if claims_exists and "source" not in columns:
        conn.execute("ALTER TABLE claims ADD COLUMN source TEXT")
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version','9') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _upgrade_to_v10(conn: sqlite3.Connection) -> None:
    notifications_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notifications'"
    ).fetchone()
    notification_columns = {row[1] for row in conn.execute("PRAGMA table_info(notifications)")} if notifications_exists else set()
    if notifications_exists and "rewritten_text" not in notification_columns:
        conn.execute("ALTER TABLE notifications ADD COLUMN rewritten_text TEXT")
    if notifications_exists and "rewrite_outcome" not in notification_columns:
        conn.execute("ALTER TABLE notifications ADD COLUMN rewrite_outcome TEXT")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bg_runs(
          run_key TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repos,
          run_dir TEXT NOT NULL, script_path TEXT, status TEXT NOT NULL,
          pid INTEGER, started_at TEXT, finished_at TEXT, exit_code INTEGER,
          progress_hint TEXT, progress_at TEXT, log_mtime TEXT,
          last_notified_status TEXT, stall_notified INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL)"""
    )
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version','10') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _upgrade_to_v11(conn: sqlite3.Connection) -> None:
    repos_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='repos'").fetchone()
    repo_columns = {row[1] for row in conn.execute("PRAGMA table_info(repos)")} if repos_exists else set()
    if repos_exists and "run_watch_globs" not in repo_columns:
        conn.execute("ALTER TABLE repos ADD COLUMN run_watch_globs TEXT")
    runs_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bg_runs'").fetchone()
    run_columns = {row[1] for row in conn.execute("PRAGMA table_info(bg_runs)")} if runs_exists else set()
    if runs_exists and "orphaned" not in run_columns:
        conn.execute("ALTER TABLE bg_runs ADD COLUMN orphaned INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS gpu_queue(
          queue_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repos,
          title TEXT NOT NULL, note TEXT,
          entrypoint TEXT, entrypoint_sha256 TEXT, working_dir TEXT, run_prefix TEXT,
          context TEXT, context_commit TEXT, created_by TEXT NOT NULL,
          status TEXT NOT NULL, mode TEXT,
          asked_at TEXT, notified_idle_at TEXT, launch_attempted_at TEXT,
          launched_run_key TEXT, launch_error TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_gpu_queue_status_created ON gpu_queue(status,created_at,queue_id)")
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version','11') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _upgrade_to_v12(conn: sqlite3.Connection) -> None:
    runs_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bg_runs'").fetchone()
    run_columns = {row[1] for row in conn.execute("PRAGMA table_info(bg_runs)")} if runs_exists else set()
    for name, declaration in (
        ("source", "TEXT NOT NULL DEFAULT 'background_run'"),
        ("process_started_at", "TEXT"),
        ("command_line", "TEXT"),
        ("title", "TEXT"),
        ("log_path", "TEXT"),
        ("watch_globs", "TEXT"),
    ):
        if runs_exists and name not in run_columns:
            conn.execute(f"ALTER TABLE bg_runs ADD COLUMN {name} {declaration}")
    if runs_exists:
        conn.execute("CREATE INDEX IF NOT EXISTS ix_bg_runs_source_status ON bg_runs(source,status)")
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version','12') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _upgrade_to_v13(conn: sqlite3.Connection) -> None:
    repos_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='repos'").fetchone()
    repo_columns = {row[1] for row in conn.execute("PRAGMA table_info(repos)")} if repos_exists else set()
    if repos_exists and "run_progress_rules" not in repo_columns:
        conn.execute("ALTER TABLE repos ADD COLUMN run_progress_rules TEXT")
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version','13') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _upgrade_to_v14(conn: sqlite3.Connection) -> None:
    queue_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpu_queue'"
    ).fetchone()
    if queue_exists:
        launching_count = conn.execute(
            "SELECT COUNT(*) FROM gpu_queue WHERE status='LAUNCHING'"
        ).fetchone()[0]
        if launching_count > 1:
            raise ValidationError("multiple_launching_rows", detail={"count": launching_count})
        queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(gpu_queue)")}
        for name in ("config_paths", "register_fingerprint", "launch_fingerprint"):
            if name not in queue_columns:
                conn.execute(f"ALTER TABLE gpu_queue ADD COLUMN {name} TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_gpu_queue_one_launching "
            "ON gpu_queue(status) WHERE status='LAUNCHING'"
        )

    runs_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bg_runs'"
    ).fetchone()
    run_columns = {row[1] for row in conn.execute("PRAGMA table_info(bg_runs)")} if runs_exists else set()
    if runs_exists and "fingerprint_json" not in run_columns:
        conn.execute("ALTER TABLE bg_runs ADD COLUMN fingerprint_json TEXT")
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version','14') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _upgrade_to_v15(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS desktop_requests(
      request_id TEXT PRIMARY KEY,
      kind TEXT NOT NULL CHECK(kind IN ('claude','codex')),
      target TEXT NOT NULL,
      actor TEXT NOT NULL,
      repo_id TEXT,
      prompt_sha256 TEXT NOT NULL,
      prompt_chars INTEGER NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('SENDING','SENT','FAILED','UNKNOWN')),
      error TEXT,
      detail_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL)""")
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version','15') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _upgrade_to_v16(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(desktop_requests)")}
    for name, definition in (
        ("origin", "TEXT NOT NULL DEFAULT 'mcp'"),
        ("reported_at", "TEXT"),
        ("reported_phase", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE desktop_requests ADD COLUMN {name} {definition}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS desktop_requests_report_idx ON desktop_requests(origin, reported_at)"
    )
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version','16') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def migrate(conn: sqlite3.Connection) -> None:
    has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    version = 0
    if has_meta:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(meta)")}
        if {"key", "value"}.issubset(columns):
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row:
                try:
                    version = int(row[0])
                except (TypeError, ValueError) as exc:
                    raise ValidationError("invalid_schema_version", detail={"value": row[0]}) from exc
    if version > SCHEMA_VERSION:
        raise ValidationError("schema_too_new", detail={"version": version, "supported": SCHEMA_VERSION})
    if version == SCHEMA_VERSION:
        return
    if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15):
        raise ValidationError("unsupported_schema_migration", detail={"from": version, "to": SCHEMA_VERSION})
    with tx(conn):
        if version in (0, 1):
            for statement in _SCHEMA:
                conn.execute(statement)
            _upgrade_to_v2(conn)
            _upgrade_to_v3(conn)
        elif version == 2:
            _upgrade_to_v3(conn)
        if version <= 3:
            _upgrade_to_v4(conn)
        if version <= 4:
            _upgrade_to_v5(conn)
        if version <= 5:
            _upgrade_to_v6(conn)
        if version <= 7:
            _upgrade_to_v7(conn)
            _upgrade_to_v8(conn)
        _upgrade_to_v9(conn)
        _upgrade_to_v10(conn)
        _upgrade_to_v11(conn)
        _upgrade_to_v12(conn)
        _upgrade_to_v13(conn)
        _upgrade_to_v14(conn)
        _upgrade_to_v15(conn)
        _upgrade_to_v16(conn)


def initialize(path: Path | str | None = None) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
