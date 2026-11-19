import tempfile
import unittest
from pathlib import Path

from labops.db import connect, migrate, tx
from labops.errors import Blocked
from labops.tasks import add_repo, create_task, open_round
from tests.helpers import make_repo


class OpenRoundRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-open-round-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        make_repo(self.repo)
        self.conn = connect(self.root / "home" / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _task_with_previous_state(self, task_id: str, state: str):
        create_task(self.conn, repo_id="toy", task_id=task_id, goal="round rule", profile={"schema": 1})
        previous = open_round(self.conn, task_id=task_id, actor="codex", role="implement")
        with tx(self.conn):
            self.conn.execute("UPDATE rounds SET state=?,head_commit=? WHERE round_id=?",
                              (state, "a" * 40, previous["round_id"]))
        return previous

    def test_claimed_and_verifying_rounds_block_new_round(self):
        for suffix, state in (("claimed", "CLAIMED"), ("verifying", "VERIFYING")):
            with self.subTest(state=state):
                task_id = f"T20261019-{suffix}"
                previous = self._task_with_previous_state(task_id, state)
                with self.assertRaises(Blocked) as caught:
                    open_round(self.conn, task_id=task_id, actor="opus", role="fix")
                self.assertEqual(caught.exception.code, "blocked")
                self.assertEqual(caught.exception.args[0], "previous_round_not_settled")
                self.assertEqual(caught.exception.detail, {"round_state": state})
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM rounds WHERE task_id=?", (task_id,)).fetchone()[0], 1)

    def test_terminal_round_states_allow_new_round_and_advance_base(self):
        for suffix, state in (("verified", "VERIFIED"), ("rejected", "REJECTED"), ("blocked", "BLOCKED")):
            with self.subTest(state=state):
                task_id = f"T20261019-terminal-{suffix}"
                self._task_with_previous_state(task_id, state)
                next_round = open_round(self.conn, task_id=task_id, actor="opus", role="fix")
                self.assertEqual(next_round["state"], "OPEN")
                self.assertEqual(next_round["base_commit"], "a" * 40)


if __name__ == "__main__":
    unittest.main()
