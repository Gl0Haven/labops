import tempfile
import unittest
from pathlib import Path

from labops.db import connect, migrate
from labops.journal import OpSpec, apply_ref_op, finalize_ref_op, get_operation, register_on_applied
from tests.helpers import commit_file, make_repo


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.head = make_repo(self.repo)
        self.conn = connect(self.root / "labops.db")
        migrate(self.conn)
        self.conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy',?,'t')", (str(self.repo.resolve()),))

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def spec(self, *, target=None, expected_old=None, idem="ref-one", callback=None):
        return OpSpec("toy", "create_ref", "refs/labops/T/r1/codex/submitted", expected_old,
                      target or self.head, idem, callback)

    def test_normal_cas_and_idempotent_reentry(self):
        spec = self.spec()
        row = apply_ref_op(self.conn, self.repo, spec)
        self.assertEqual(row["status"], "APPLIED")
        again = apply_ref_op(self.conn, self.repo, spec)
        self.assertEqual(again["op_id"], row["op_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE entity_type='operation'").fetchone()[0], 1)

    def test_invalid_target_is_failed(self):
        row = apply_ref_op(self.conn, self.repo, self.spec(target="not-a-commit", idem="invalid"))
        self.assertEqual(row["status"], "FAILED")
        self.assertIn("target_not_commit", row["detail_json"])

    def test_cas_conflict_is_recorded_without_moving_ref(self):
        other = commit_file(self.repo, "other.txt", "other\n")
        ref = "refs/labops/T/r1/codex/submitted"
        from labops.gitio import update_ref_cas, zero_oid
        self.assertTrue(update_ref_cas(self.repo, ref, other, zero_oid(self.repo)))
        row = apply_ref_op(self.conn, self.repo, self.spec(idem="conflict"))
        self.assertEqual(row["status"], "CONFLICT")
        from labops.gitio import read_ref
        self.assertEqual(read_ref(self.repo, ref), other)

    def test_on_applied_callback_is_idempotent_and_in_same_commit(self):
        self.conn.execute("CREATE TABLE callback_marks(name TEXT PRIMARY KEY, count INTEGER NOT NULL)")
        def callback(conn, params):
            prior = conn.execute("SELECT count FROM callback_marks WHERE name=?", (params["name"],)).fetchone()
            if prior:
                return
            conn.execute("INSERT INTO callback_marks VALUES(?,1)", (params["name"],))
        register_on_applied("test_mark", callback)
        row = apply_ref_op(self.conn, self.repo, self.spec(idem="callback", callback={"type": "test_mark", "name": "once"}))
        self.assertEqual(row["status"], "APPLIED")
        self.assertEqual(self.conn.execute("SELECT count FROM callback_marks WHERE name='once'").fetchone()[0], 1)
        self.assertEqual(finalize_ref_op(self.conn, self.repo, row["op_id"], cas_ok=None)["status"], "APPLIED")
        self.assertEqual(self.conn.execute("SELECT count FROM callback_marks WHERE name='once'").fetchone()[0], 1)
        self.assertEqual(get_operation(self.conn, row["op_id"])["status"], "APPLIED")


if __name__ == "__main__":
    unittest.main()
