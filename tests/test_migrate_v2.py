import json
import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path

from labops.canon import canonical_json
from labops.db import migrate

_FIXTURES = Path(__file__).with_name("fixtures")


def _restore_fixture(root: Path, version: int) -> Path:
    path = root / f"legacy_v{version}.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript((_FIXTURES / f"legacy_v{version}.sql").read_text(encoding="utf-8"))
    finally:
        conn.close()
    return path


def _make_m0a_v1_db(root: Path) -> tuple[sqlite3.Connection, str]:
    conn = sqlite3.connect(_restore_fixture(root, 1), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    expected_policy = canonical_json({
        "schema": 1, "frozen": [{"path": "src/frozen.py", "symbols": ["f"], "ref": "review", "note": "M0a fixture"}],
    }).decode()
    return conn, expected_policy


def _make_v2_db(root: Path) -> Path:
    return _restore_fixture(root, 2)


def _make_v3_db_copy(root: Path) -> Path:
    return _restore_fixture(root, 3)


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables}


class MigrateV2Tests(unittest.TestCase):
    def test_v7_migration_adds_nullable_ntfy_status_and_preserves_rows(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES('schema_version','7')")
        conn.execute("CREATE TABLE notifications(event_key TEXT PRIMARY KEY,task_id TEXT,kind TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,created_at TEXT NOT NULL,sent_at TEXT)")
        conn.execute("INSERT INTO notifications(event_key,kind,text,status,created_at) VALUES('ev:1','test','first line\nsecond line','PENDING','2026-10-20T00:00:00Z')")
        try:
            migrate(conn)
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
            self.assertIn("ntfy_status", {row[1] for row in conn.execute("PRAGMA table_info(notifications)")})
            row = conn.execute("SELECT text,status,attempts,ntfy_status FROM notifications WHERE event_key='ev:1'").fetchone()
            self.assertEqual(tuple(row), ("first line\nsecond line", "PENDING", 0, None))
            migrate(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)
        finally:
            conn.close()

    def test_v6_migration_adds_alert_detail_default_and_repeat_is_noop(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES('schema_version','6')")
        conn.execute("CREATE TABLE repos(repo_id TEXT PRIMARY KEY, confidential INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO repos(repo_id,confidential) VALUES('toy',1)")
        try:
            migrate(conn)
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
            self.assertEqual(conn.execute("SELECT alert_detail FROM repos WHERE repo_id='toy'").fetchone()[0], 1)
            meta = [tuple(row) for row in conn.execute("SELECT key,value FROM meta ORDER BY key")]
            migrate(conn)
            self.assertEqual([tuple(row) for row in conn.execute("SELECT key,value FROM meta ORDER BY key")], meta)
            self.assertEqual(conn.execute("SELECT alert_detail FROM repos WHERE repo_id='toy'").fetchone()[0], 1)
        finally:
            conn.close()

    def test_v4_migration_rebuilds_legacy_records_and_creates_search_tables(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES('schema_version','4')")
        conn.execute("CREATE TABLE repos(repo_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO repos VALUES('toy')")
        conn.execute(
            """CREATE TABLE legacy_records(
              repo_id TEXT NOT NULL REFERENCES repos,kind TEXT NOT NULL,key TEXT NOT NULL,path TEXT NOT NULL,
              line INTEGER,title TEXT,value TEXT,content_hash TEXT NOT NULL,imported_at TEXT NOT NULL,
              PRIMARY KEY(repo_id,kind,key))"""
        )
        conn.execute("INSERT INTO legacy_records VALUES('toy','decision','D-20261020-1','x.md',1,'title',NULL,'hash','t')")
        try:
            migrate(conn)
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM legacy_records").fetchone()[0], 0)
            self.assertIn("ref", {row[1] for row in conn.execute("PRAGMA table_info(legacy_records)")})
            self.assertEqual(conn.execute("SELECT alert_detail FROM repos WHERE repo_id='toy'").fetchone()[0], 1)
            for name in ("search_docs", "search_tri", "search_bi"):
                self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone())
            migrate(conn)
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
        finally:
            conn.close()

    def test_m0a_v1_policy_snapshot_is_migrated_exactly_and_repeat_is_noop(self):
        with tempfile.TemporaryDirectory(prefix="labops-migrate-v2-") as temporary:
            conn, expected = _make_m0a_v1_db(Path(temporary))
            try:
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "1")
                event_before = conn.execute("SELECT detail_json FROM events WHERE entity_id='T20261019-migrate'").fetchone()[0]
                migrate(conn)
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
                self.assertEqual(conn.execute("SELECT policy_json FROM tasks WHERE task_id='T20261019-migrate'").fetchone()[0], expected)
                self.assertEqual(conn.execute("SELECT detail_json FROM events WHERE entity_id='T20261019-migrate'").fetchone()[0], event_before)
                self.assertIn("policy_json", {row[1] for row in conn.execute("PRAGMA table_info(tasks)")})
                self.assertIn("confidential", {row[1] for row in conn.execute("PRAGMA table_info(repos)")})
                self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='spool_events'").fetchone())
                self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notifications'").fetchone())
                self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='backups'").fetchone())
                self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_records'").fetchone())
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='notify_cursor'").fetchone()[0],
                                 str(conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]))
                before = [tuple(row) for row in conn.execute("SELECT key,value FROM meta ORDER BY key")]
                migrate(conn)
                self.assertEqual([tuple(row) for row in conn.execute("SELECT key,value FROM meta ORDER BY key")], before)
                self.assertEqual(json.loads(event_before)["frozen_policy"], json.loads(expected))
            finally:
                conn.close()

    def test_real_v2_database_copy_migrates_without_losing_rows_and_repeat_is_noop(self):
        with tempfile.TemporaryDirectory(prefix="labops-migrate-v3-") as temporary:
            source = _make_v2_db(Path(temporary))
            copied = Path(temporary) / "v2-copy.db"
            shutil.copy2(source, copied)
            conn = sqlite3.connect(copied, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "2")
                before = _row_counts(conn)
                max_seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
                migrate(conn)
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
                after = _row_counts(conn)
                self.assertTrue(all(after[name] == count for name, count in before.items() if name != "meta"))
                self.assertEqual(after["notifications"], 0)
                self.assertEqual(conn.execute("SELECT confidential FROM repos WHERE repo_id='toy-v2'").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='notify_cursor'").fetchone()[0], str(max_seq))
                stable_meta = [tuple(row) for row in conn.execute("SELECT key,value FROM meta ORDER BY key")]
                migrate(conn)
                self.assertEqual([tuple(row) for row in conn.execute("SELECT key,value FROM meta ORDER BY key")], stable_meta)
                self.assertEqual(_row_counts(conn), after)
            finally:
                conn.close()

    def test_empty_database_migrates_directly_to_v8(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            migrate(conn)
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM desktop_requests").fetchone()[0], 0)
            self.assertIn("policy_json", {row[1] for row in conn.execute("PRAGMA table_info(tasks)")})
            self.assertIn("brief", {row[1] for row in conn.execute("PRAGMA table_info(tasks)")})
            self.assertIsNotNone(conn.execute("SELECT value FROM meta WHERE key='handoff_rules'").fetchone())
            self.assertIn("python", {row[1] for row in conn.execute("PRAGMA table_info(repos)")})
            self.assertIn("alert_detail", {row[1] for row in conn.execute("PRAGMA table_info(repos)")})
        finally:
            conn.close()

    def test_live_v3_database_copy_migrates_to_v8_and_repeat_is_noop(self):
        with tempfile.TemporaryDirectory(prefix="labops-migrate-live-v3-") as temporary:
            copied = _make_v3_db_copy(Path(temporary))
            conn = sqlite3.connect(copied, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "3")
                before = _row_counts(conn)
                migrate(conn)
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "16")
                after = _row_counts(conn)
                self.assertEqual(after["tasks"], before["tasks"])
                self.assertEqual(after["rounds"], before["rounds"])
                self.assertEqual(conn.execute("SELECT confidential FROM repos WHERE repo_id='toy-v3'").fetchone()[0], 1)
                columns = {row[1] for row in conn.execute("PRAGMA table_info(repos)")}
                self.assertTrue({"python", "backup_root", "backup_keep", "legacy_bridge"}.issubset(columns))
                self.assertIn("alert_detail", columns)
                before_repeat = _row_counts(conn)
                migrate(conn)
                self.assertEqual(_row_counts(conn), before_repeat)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
