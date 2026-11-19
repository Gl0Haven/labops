import sqlite3
import tempfile
import unittest
from pathlib import Path

from labops.db import _upgrade_to_v15, connect, migrate, tx
from labops.errors import ValidationError


class DatabaseTests(unittest.TestCase):
    def test_schema_v1_pragmas_and_idempotent_migration(self):
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "labops.db")
            migrate(conn)
            migrate(conn)
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual({r[1] for r in conn.execute("PRAGMA table_info(rounds)")},
                             {"round_id", "task_id", "n", "actor", "role", "base_commit", "state", "head_commit", "head_tree", "branch", "worktree_path", "claim_id", "created_at", "updated_at"})
            self.assertIn("policy_json", {r[1] for r in conn.execute("PRAGMA table_info(tasks)")})
            self.assertIn("brief", {r[1] for r in conn.execute("PRAGMA table_info(tasks)")})
            self.assertIsNotNone(conn.execute("SELECT 1 FROM meta WHERE key='handoff_rules'").fetchone())
            self.assertIn("confidential", {r[1] for r in conn.execute("PRAGMA table_info(repos)")})
            self.assertIn("alert_detail", {r[1] for r in conn.execute("PRAGMA table_info(repos)")})
            self.assertIn("run_watch_globs", {r[1] for r in conn.execute("PRAGMA table_info(repos)")})
            self.assertIn("run_progress_rules", {r[1] for r in conn.execute("PRAGMA table_info(repos)")})
            conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy','toy-path','t')")
            self.assertEqual(conn.execute("SELECT alert_detail FROM repos WHERE repo_id='toy'").fetchone()[0], 1)
            self.assertTrue({"python", "backup_root", "backup_keep", "legacy_bridge"}.issubset(
                {r[1] for r in conn.execute("PRAGMA table_info(repos)")}))
            self.assertIn("ref", {r[1] for r in conn.execute("PRAGMA table_info(legacy_records)")})
            for name in ("search_docs", "search_tri", "search_bi"):
                self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='spool_events'").fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notifications'").fetchone())
            self.assertIn("rewritten_text", {r[1] for r in conn.execute("PRAGMA table_info(notifications)")})
            self.assertIn("rewrite_outcome", {r[1] for r in conn.execute("PRAGMA table_info(notifications)")})
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bg_runs'").fetchone())
            self.assertIn("orphaned", {r[1] for r in conn.execute("PRAGMA table_info(bg_runs)")})
            self.assertTrue({"source", "process_started_at", "command_line", "title", "log_path", "watch_globs"}.issubset(
                {r[1] for r in conn.execute("PRAGMA table_info(bg_runs)")}))
            self.assertIn("fingerprint_json", {r[1] for r in conn.execute("PRAGMA table_info(bg_runs)")})
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpu_queue'").fetchone())
            self.assertTrue({"config_paths", "register_fingerprint", "launch_fingerprint"}.issubset(
                {r[1] for r in conn.execute("PRAGMA table_info(gpu_queue)")}))
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_gpu_queue_one_launching'"
            ).fetchone())
            conn.close()

    def test_migration_from_v9_to_v14_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "labops.db")
            migrate(conn)
            conn.execute("ALTER TABLE notifications DROP COLUMN rewritten_text")
            conn.execute("ALTER TABLE notifications DROP COLUMN rewrite_outcome")
            conn.execute("DROP TABLE bg_runs")
            conn.execute("DROP TABLE gpu_queue")
            conn.execute("ALTER TABLE repos DROP COLUMN run_watch_globs")
            conn.execute("UPDATE meta SET value='9' WHERE key='schema_version'")

            migrate(conn)
            first_columns = {row[1] for row in conn.execute("PRAGMA table_info(notifications)")}
            first_run_table = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='bg_runs'"
            ).fetchone()[0]
            migrate(conn)
            second_columns = {row[1] for row in conn.execute("PRAGMA table_info(notifications)")}
            second_run_table = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='bg_runs'"
            ).fetchone()[0]
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
            self.assertTrue({"rewritten_text", "rewrite_outcome"}.issubset(first_columns))
            self.assertEqual(first_columns, second_columns)
            self.assertEqual((first_run_table, second_run_table), (1, 1))
            self.assertIn("run_watch_globs", {row[1] for row in conn.execute("PRAGMA table_info(repos)")})
            self.assertIn("orphaned", {row[1] for row in conn.execute("PRAGMA table_info(bg_runs)")})
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpu_queue'").fetchone())
            conn.close()

    def test_migration_from_v10_to_v14_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "labops.db")
            migrate(conn)
            conn.execute("ALTER TABLE repos DROP COLUMN run_watch_globs")
            conn.execute("ALTER TABLE bg_runs DROP COLUMN orphaned")
            conn.execute("DROP TABLE gpu_queue")
            conn.execute("UPDATE meta SET value='10' WHERE key='schema_version'")

            migrate(conn)
            migrate(conn)
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
            self.assertIn("run_watch_globs", {row[1] for row in conn.execute("PRAGMA table_info(repos)")})
            self.assertIn("orphaned", {row[1] for row in conn.execute("PRAGMA table_info(bg_runs)")})
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpu_queue'").fetchone())
            conn.close()

    def test_migration_from_v11_adds_adopted_run_columns_once(self):
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "labops.db")
            migrate(conn)
            conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy','toy-path','t')")
            conn.execute(
                "INSERT INTO bg_runs(run_key,repo_id,run_dir,status,updated_at) VALUES(?,?,?,?,?)",
                ("b" * 64, "toy", "runs/existing", "running", "t"),
            )
            conn.execute("DROP INDEX IF EXISTS ix_bg_runs_source_status")
            for column in ("source", "process_started_at", "command_line", "title", "log_path", "watch_globs"):
                conn.execute(f"ALTER TABLE bg_runs DROP COLUMN {column}")
            conn.execute("UPDATE meta SET value='11' WHERE key='schema_version'")

            migrate(conn)
            migrate(conn)

            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(bg_runs)")}
            self.assertTrue({"source", "process_started_at", "command_line", "title", "log_path", "watch_globs"}.issubset(columns))
            self.assertEqual(conn.execute("SELECT source FROM bg_runs WHERE run_key=?", ("b" * 64,)).fetchone()[0],
                             "background_run")
            conn.close()

    def test_migration_from_v12_adds_progress_rules_once_and_preserves_repo_data(self):
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "labops.db")
            migrate(conn)
            conn.execute("INSERT INTO repos(repo_id,path,run_watch_globs,created_at) "
                         "VALUES('toy','toy-path','[\"logs/*.jsonl\"]','t')")
            conn.execute("ALTER TABLE repos DROP COLUMN run_progress_rules")
            conn.execute("UPDATE meta SET value='12' WHERE key='schema_version'")

            migrate(conn)
            migrate(conn)

            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(repos)")}
            self.assertIn("run_progress_rules", columns)
            row = conn.execute("SELECT run_watch_globs,run_progress_rules FROM repos WHERE repo_id='toy'").fetchone()
            self.assertEqual(row["run_watch_globs"], '["logs/*.jsonl"]')
            self.assertIsNone(row["run_progress_rules"])
            conn.close()

    def test_migration_from_v13_adds_fingerprint_columns_and_launch_index_once(self):
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "labops.db")
            migrate(conn)
            conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy','toy-path','t')")
            conn.execute(
                "INSERT INTO gpu_queue(queue_id,repo_id,title,note,created_by,status,created_at,updated_at) "
                "VALUES('q1','toy','one','','codex','QUEUED','t','t')"
            )
            conn.execute(
                "INSERT INTO bg_runs(run_key,repo_id,run_dir,status,updated_at) VALUES(?,?,?,?,?)",
                ("c" * 64, "toy", "runs/existing", "running", "t"),
            )
            conn.execute("DROP INDEX ux_gpu_queue_one_launching")
            for column in ("config_paths", "register_fingerprint", "launch_fingerprint"):
                conn.execute(f"ALTER TABLE gpu_queue DROP COLUMN {column}")
            conn.execute("ALTER TABLE bg_runs DROP COLUMN fingerprint_json")
            conn.execute("UPDATE meta SET value='13' WHERE key='schema_version'")

            migrate(conn)
            migrate(conn)

            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
            queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(gpu_queue)")}
            run_columns = {row[1] for row in conn.execute("PRAGMA table_info(bg_runs)")}
            self.assertTrue({"config_paths", "register_fingerprint", "launch_fingerprint"}.issubset(queue_columns))
            self.assertIn("fingerprint_json", run_columns)
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_gpu_queue_one_launching'"
            ).fetchone())
            self.assertEqual(conn.execute("SELECT status FROM gpu_queue WHERE queue_id='q1'").fetchone()[0], "QUEUED")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bg_runs WHERE run_key=?", ("c" * 64,)).fetchone()[0], 1)
            conn.execute(
                "INSERT INTO gpu_queue(queue_id,repo_id,title,note,created_by,status,created_at,updated_at) "
                "VALUES('q2','toy','two','','codex','QUEUED','t','t')"
            )
            conn.execute("UPDATE gpu_queue SET status='LAUNCHING' WHERE queue_id='q1'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE gpu_queue SET status='LAUNCHING' WHERE queue_id='q2'")
            conn.close()

    def test_v13_migration_stops_on_multiple_launching_rows_without_repair(self):
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "labops.db")
            migrate(conn)
            conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy','toy-path','t')")
            for queue_id in ("q1", "q2"):
                conn.execute(
                    "INSERT INTO gpu_queue(queue_id,repo_id,title,note,created_by,status,created_at,updated_at) "
                    "VALUES(?, 'toy', ?, '', 'codex', 'QUEUED', 't', 't')",
                    (queue_id, queue_id),
                )
            conn.execute("DROP INDEX ux_gpu_queue_one_launching")
            for column in ("config_paths", "register_fingerprint", "launch_fingerprint"):
                conn.execute(f"ALTER TABLE gpu_queue DROP COLUMN {column}")
            conn.execute("ALTER TABLE bg_runs DROP COLUMN fingerprint_json")
            conn.execute("UPDATE meta SET value='13' WHERE key='schema_version'")
            conn.execute("UPDATE gpu_queue SET status='LAUNCHING'")

            with self.assertRaisesRegex(ValidationError, "multiple_launching_rows"):
                migrate(conn)

            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "13")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM gpu_queue WHERE status='LAUNCHING'").fetchone()[0], 2)
            self.assertNotIn("launch_fingerprint", {row[1] for row in conn.execute("PRAGMA table_info(gpu_queue)")})
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_gpu_queue_one_launching'"
            ).fetchone())
            conn.close()

    def test_transaction_rolls_back_and_foreign_keys_are_enforced(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        migrate(conn)
        with self.assertRaises(RuntimeError):
            with tx(conn):
                conn.execute("INSERT INTO meta(key,value) VALUES('temp','x')")
                raise RuntimeError("rollback")
        self.assertIsNone(conn.execute("SELECT value FROM meta WHERE key='temp'").fetchone())
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO tasks(task_id,repo_id,goal,state,base_commit,profile_hash,created_at,updated_at) VALUES('T20261019-x','missing','g','CREATED','a','p','t','t')")
        conn.close()

    def test_v14_migration_adds_desktop_requests_and_is_idempotent(self):
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        migrate(conn)
        conn.execute("DROP TABLE desktop_requests")
        conn.execute("UPDATE meta SET value='14' WHERE key='schema_version'")
        conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy','toy-path','t')")
        migrate(conn)
        migrate(conn)
        self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
        self.assertEqual({row[1] for row in conn.execute("PRAGMA table_info(desktop_requests)")},
                         {"request_id", "kind", "target", "actor", "repo_id", "prompt_sha256",
                          "prompt_chars", "state", "error", "detail_json", "created_at", "updated_at",
                          "origin", "reported_at", "reported_phase"})
        self.assertEqual(conn.execute("SELECT path FROM repos WHERE repo_id='toy'").fetchone()[0], "toy-path")

    def test_v15_migration_preserves_desktop_requests_and_is_idempotent(self):
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        migrate(conn)
        conn.execute("DROP TABLE desktop_requests")
        _upgrade_to_v15(conn)
        conn.execute(
            "INSERT INTO desktop_requests(request_id,kind,target,actor,prompt_sha256,prompt_chars,"
            "state,detail_json,created_at,updated_at) "
            "VALUES('test-req','claude','session','opus','sha',1,'SENT','{}','t','t')"
        )

        migrate(conn)
        migrate(conn)
        self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
        self.assertTrue({"origin", "reported_at", "reported_phase"}.issubset(
            {row[1] for row in conn.execute("PRAGMA table_info(desktop_requests)")}))
        self.assertEqual(tuple(conn.execute(
            "SELECT origin,reported_at,reported_phase,state FROM desktop_requests WHERE request_id='test-req'"
        ).fetchone()), ("mcp", None, None, "SENT"))
        self.assertEqual([row[2] for row in conn.execute("PRAGMA index_info(desktop_requests_report_idx)")],
                         ["origin", "reported_at"])


    def test_rejects_schema_ahead(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES('schema_version','17')")
        with self.assertRaisesRegex(ValidationError, "schema_too_new"):
            migrate(conn)
        conn.close()


if __name__ == "__main__":
    unittest.main()
