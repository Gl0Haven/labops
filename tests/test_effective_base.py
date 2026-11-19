import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops.db import connect, migrate
from labops.gate import evaluate
from labops.gitio import read_ref, rev_parse
from labops.tasks import (add_repo, create_task, create_worktree, open_round, set_policy, snapshot,
                          submit_changes, sync_main)
from tests.helpers import commit_file, make_repo


class EffectiveBaseTests(unittest.TestCase):
    """Gate scope checks after sync_main, and first rounds starting from the current main."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        make_repo(self.repo, files={"calc.py": "def add(a,b):\n    return a+b\n", "src/work.py": "X = 1\n",
                                    "docs/notes.md": "base\n"})
        self.env = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.env.start()
        self.conn = connect(self.home / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo, worktree_root=self.root / "worktrees")
        set_policy(self.conn, "toy", {"schema": 1, "frozen": [
            {"path": "calc.py", "symbols": None, "ref": "D-test", "note": "frozen calc"}]})
        self.profile = {"schema": 1, "allowed_paths": ["src/**"],
                        "commands": [{"name": "unit", "argv": ["{python}", "-c", "pass"], "cwd": ".", "timeout_s": 60}]}

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def test_sync_main_does_not_attribute_other_tasks_changes(self):
        create_task(self.conn, repo_id="toy", task_id="T20261021-eff", goal="eff", profile=self.profile)
        rnd = open_round(self.conn, task_id="T20261021-eff", actor="codex", role="implement")
        wt = Path(create_worktree(self.conn, rnd["round_id"])["path"])
        wt.joinpath("src", "work.py").write_text("X = 2\n", encoding="utf-8")
        snapshot(self.conn, round_id=rnd["round_id"], files=["src/work.py"], message="own work", actor="codex")
        # Another task integrates changes to a frozen file and an out-of-scope path on main.
        commit_file(self.repo, "calc.py", "def add(a,b):\n    return b+a\n", "other task frozen edit")
        commit_file(self.repo, "docs/notes.md", "other\n", "other task docs")
        main_tip = read_ref(self.repo, "refs/heads/main") or rev_parse(self.repo, "HEAD")
        merged = sync_main(self.conn, round_id=rnd["round_id"], actor="codex")
        self.assertTrue(merged["merged"])
        submit_changes(self.conn, round_id=rnd["round_id"], actor="codex", commit=merged["commit"],
                       client_request_id="eff")
        result = evaluate(self.conn, "T20261021-eff")
        self.assertEqual(result["detail"]["scope_base"], main_tip)
        self.assertEqual(result["detail"]["hits"], [])
        self.assertEqual(result["detail"]["out"], [])
        self.assertNotIn("frozen_zone_touched", result["reasons"])
        self.assertNotIn("out_of_scope", result["reasons"])

    def test_own_out_of_scope_change_is_still_reported_after_sync(self):
        create_task(self.conn, repo_id="toy", task_id="T20261021-eff2", goal="eff2", profile=self.profile)
        rnd = open_round(self.conn, task_id="T20261021-eff2", actor="codex", role="implement")
        wt = Path(create_worktree(self.conn, rnd["round_id"])["path"])
        wt.joinpath("calc.py").write_text("def add(a,b):\n    return a+b+0\n", encoding="utf-8")
        snapshot(self.conn, round_id=rnd["round_id"], files=["calc.py"], message="own frozen edit", actor="codex")
        commit_file(self.repo, "docs/notes.md", "other\n", "other task docs")
        merged = sync_main(self.conn, round_id=rnd["round_id"], actor="codex")
        submit_changes(self.conn, round_id=rnd["round_id"], actor="codex", commit=merged["commit"],
                       client_request_id="eff2")
        result = evaluate(self.conn, "T20261021-eff2")
        self.assertIn("frozen_zone_touched", result["reasons"])
        self.assertIn("out_of_scope", result["reasons"])
        self.assertNotIn("docs/notes.md", result["detail"]["out"])

    def test_first_round_keeps_task_base_but_flags_main_ahead(self):
        # The base is never moved silently: a direct commit on main must not be absorbed into it.
        create_task(self.conn, repo_id="toy", task_id="T20261021-eff3", goal="eff3", profile=self.profile)
        task_base = self.conn.execute("SELECT base_commit FROM tasks WHERE task_id='T20261021-eff3'").fetchone()[0]
        commit_file(self.repo, "docs/notes.md", "later\n", "main advanced")
        rnd = open_round(self.conn, task_id="T20261021-eff3", actor="codex", role="implement")
        self.assertEqual(rnd["base_commit"], task_base)
        self.assertTrue(rnd["main_ahead"])
        self.assertIn("sync_main", rnd["hint"])

    def test_first_round_without_main_movement_has_no_hint(self):
        create_task(self.conn, repo_id="toy", task_id="T20261021-eff4", goal="eff4", profile=self.profile)
        rnd = open_round(self.conn, task_id="T20261021-eff4", actor="codex", role="implement")
        self.assertNotIn("main_ahead", rnd)


if __name__ == "__main__":
    unittest.main()
