import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops.backup import run_backup, verify_restore
from labops.db import connect, migrate
from labops.errors import Blocked, GitConflict
from labops.gitio import commit_trailers, is_ancestor, read_ref, rev_parse, run as git_run, status_porcelain
from labops.reconcile import reconcile
from labops.report import stats_report
from labops.tasks import (add_repo, create_task, create_worktree, open_round, set_repo_options, snapshot,
                          submit_changes, sync_main)
from tests.helpers import make_repo


def _commit_on_main(repo: Path, name: str, content: str, message: str) -> str:
    repo.joinpath(name).write_text(content, encoding="utf-8")
    git_run(repo, "add", "--", name, read_only=False)
    git_run(repo, "commit", "-m", message, read_only=False)
    return rev_parse(repo, "HEAD")


class SyncMainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "toy"
        make_repo(self.repo, files={"calc.py": "def add(a,b):\n    return a+b\n", "plan.md": "plan v1\n"})
        self.env_patch = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.env_patch.start()
        self.conn = connect(self.home / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo, worktree_root=self.root / "worktrees")
        self.task_id = "T20261020-sync"
        create_task(self.conn, repo_id="toy", task_id=self.task_id, goal="sync", profile={"schema": 1})
        self.round = open_round(self.conn, task_id=self.task_id, actor="codex", role="implement")
        self.wt = Path(create_worktree(self.conn, self.round["round_id"])["path"])

    def tearDown(self):
        self.conn.close()
        self.env_patch.stop()
        self.temp.cleanup()

    def _work(self, content="def add(a,b):\n    return b+a\n"):
        self.wt.joinpath("calc.py").write_text(content, encoding="utf-8")
        return snapshot(self.conn, round_id=self.round["round_id"], files=["calc.py"], message="work", actor="codex")

    def test_noop_when_round_already_contains_main(self):
        self._work()
        result = sync_main(self.conn, round_id=self.round["round_id"], actor="codex")
        self.assertFalse(result["merged"])
        self.assertEqual(result["reason"], "already_contains_main")

    def test_merges_advanced_main_and_is_attributed(self):
        work = self._work()["commit"]
        main_tip = _commit_on_main(self.repo, "plan.md", "plan v2\n", "plan addendum")
        result = sync_main(self.conn, round_id=self.round["round_id"], actor="codex")
        self.assertTrue(result["merged"])
        head = rev_parse(self.wt, "HEAD")
        self.assertEqual(result["commit"], head)
        self.assertTrue(is_ancestor(self.repo, main_tip, head))
        self.assertTrue(is_ancestor(self.repo, work, head))
        self.assertEqual(commit_trailers(self.wt, head)["LabOps-Task"], self.task_id)
        self.assertEqual(status_porcelain(self.wt), [])
        reconcile(self.conn, repo_id="toy")
        flagged = self.conn.execute("SELECT COUNT(*) FROM events WHERE to_state='unattributed_commit'").fetchone()[0]
        self.assertEqual(flagged, 0)
        replay = sync_main(self.conn, round_id=self.round["round_id"], actor="codex")
        self.assertFalse(replay["merged"])
        claim = submit_changes(self.conn, round_id=self.round["round_id"], actor="codex", commit=head,
                               client_request_id="r1", source="mcp")
        self.assertEqual(claim["status"], "accepted")

    def test_conflict_aborts_and_leaves_worktree_clean(self):
        self._work("def add(a,b):\n    return a+b+0\n")
        _commit_on_main(self.repo, "calc.py", "def add(a,b):\n    return a+b+1\n", "conflicting main")
        head_before = rev_parse(self.wt, "HEAD")
        with self.assertRaises(GitConflict):
            sync_main(self.conn, round_id=self.round["round_id"], actor="codex")
        self.assertEqual(rev_parse(self.wt, "HEAD"), head_before)
        self.assertEqual(status_porcelain(self.wt), [])
        status = self.conn.execute("SELECT status FROM operations WHERE idempotency_id GLOB 'snap:*:sync:*'").fetchone()[0]
        self.assertEqual(status, "FAILED")

    def test_dirty_worktree_and_wrong_actor_are_refused(self):
        _commit_on_main(self.repo, "plan.md", "plan v2\n", "plan addendum")
        self.wt.joinpath("calc.py").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(Blocked):
            sync_main(self.conn, round_id=self.round["round_id"], actor="codex")
        with self.assertRaises(Exception):
            sync_main(self.conn, round_id=self.round["round_id"], actor="opus")

    def test_claim_source_and_stats_share(self):
        head = self._work()["commit"]
        submit_changes(self.conn, round_id=self.round["round_id"], actor="codex", commit=head,
                       client_request_id="r1", source="mcp")
        source = self.conn.execute("SELECT source FROM claims WHERE kind='submit_changes'").fetchone()[0]
        self.assertEqual(source, "mcp")
        submissions = stats_report(self.conn)["submissions"]
        self.assertEqual(submissions["by_source"], {"mcp": 1})
        self.assertEqual(submissions["mcp_claim_share"], 1.0)

    def test_verify_restore_is_persisted_for_stats(self):
        set_repo_options(self.conn, "toy", backup_root=str(self.root / "backups"))
        run_backup(self.conn, "toy")
        result = verify_restore(self.conn, "toy")
        record = json.loads(self.conn.execute("SELECT value FROM meta WHERE key='last_verify_restore'").fetchone()[0])
        self.assertEqual(record["backup_id"], result["backup_id"])
        self.assertEqual(record["ok"], result["ok"])
        self.assertEqual(stats_report(self.conn)["repositories"]["last_verify_restore"]["repo_id"], "toy")


if __name__ == "__main__":
    unittest.main()
