import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import json

from labops.db import connect, migrate
from labops.reconcile import reconcile
from labops.tasks import add_repo, create_task, create_worktree, open_round, snapshot, submit_changes
from labops.worker import run_once
from tests.helpers import commit_file, make_repo

RUNNABLE_PROFILE = {"schema": 1, "commands": [{"name": "unit", "argv": ["{python}", "-c", "pass"], "cwd": ".", "timeout_s": 60}]}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-worker-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.env_patch = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.env_patch.start()
        self.conn = connect(self.home / "labops.db")
        migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.env_patch.stop()
        self.temp.cleanup()

    def _claimed_task(self, repo_id: str, repo: Path, task_id: str, trusted: bool):
        add_repo(self.conn, repo_id=repo_id, path=repo, worktree_root=self.root / "worktrees" / repo_id,
                 trusted_for_exec=trusted)
        create_task(self.conn, repo_id=repo_id, task_id=task_id, goal="worker", profile=RUNNABLE_PROFILE)
        rnd = open_round(self.conn, task_id=task_id, actor="codex", role="implement")
        head = commit_file(repo, f"{repo_id}-result.txt", "result\n")
        submit_changes(self.conn, round_id=rnd["round_id"], actor="codex", commit=head,
                       client_request_id=f"submit-{repo_id}")
        return rnd, head

    def test_worker_verifies_trusted_claimed_round_and_skips_untrusted_repo(self):
        trusted_repo = self.root / "trusted"
        untrusted_repo = self.root / "untrusted"
        make_repo(trusted_repo)
        make_repo(untrusted_repo)
        trusted, _ = self._claimed_task("trusted", trusted_repo, "T20261019-worker-trusted", True)
        untrusted, _ = self._claimed_task("untrusted", untrusted_repo, "T20261019-worker-untrusted", False)
        result = run_once(self.conn)
        verified = {item["round_id"]: item for item in result["verified"]}
        self.assertEqual(verified[trusted["round_id"]]["result"]["state"], "VERIFIED")
        self.assertNotIn(untrusted["round_id"], verified)
        self.assertEqual(self.conn.execute("SELECT state FROM rounds WHERE round_id=?", (untrusted["round_id"],)).fetchone()[0], "CLAIMED")

    def test_unattributed_branch_tip_is_recorded_once_without_state_change(self):
        repo = self.root / "repo"
        base = make_repo(repo)
        add_repo(self.conn, repo_id="toy", path=repo, worktree_root=self.root / "worktrees" / "toy")
        create_task(self.conn, repo_id="toy", task_id="T20261019-unattributed", goal="unattributed", profile={"schema": 1})
        rnd = open_round(self.conn, task_id="T20261019-unattributed", actor="codex", role="implement")
        worktree = create_worktree(self.conn, rnd["round_id"])
        commit_file(Path(worktree["path"]), "unclaimed.txt", "unclaimed\n", "unattributed")
        first = reconcile(self.conn, "toy")
        self.assertEqual(first["unattributed_commits"], 1)
        row = self.conn.execute("SELECT state FROM rounds WHERE round_id=?", (rnd["round_id"],)).fetchone()
        self.assertEqual(row["state"], "OPEN")
        event = self.conn.execute("SELECT detail_json FROM events WHERE entity_id=? AND to_state='unattributed_commit'",
                                  (rnd["round_id"],)).fetchone()
        detail = json.loads(event[0])
        self.assertEqual(detail["expected"], base)
        self.assertNotEqual(detail["tip"], base)
        second = reconcile(self.conn, "toy")
        self.assertEqual(second["unattributed_commits"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE entity_id=? AND to_state='unattributed_commit'",
                                           (rnd["round_id"],)).fetchone()[0], 1)

    def test_snapshot_tip_is_attributed_until_a_later_manual_commit(self):
        repo = self.root / "repo-snapshot"
        base = make_repo(repo)
        add_repo(self.conn, repo_id="snapshot", path=repo, worktree_root=self.root / "worktrees" / "snapshot")
        create_task(self.conn, repo_id="snapshot", task_id="T20261019-snapshot-attribution",
                    goal="snapshot attribution", profile={"schema": 1})
        rnd = open_round(self.conn, task_id="T20261019-snapshot-attribution", actor="codex", role="implement")
        worktree = create_worktree(self.conn, rnd["round_id"])
        wt = Path(worktree["path"])
        (wt / "snap.txt").write_text("snapshot\n", encoding="utf-8")
        snapshot_result = snapshot(self.conn, round_id=rnd["round_id"], files=["snap.txt"],
                                   message="snapshot", actor="codex")
        after_snapshot = reconcile(self.conn, "snapshot")
        self.assertEqual(after_snapshot["unattributed_commits"], 0)
        self.assertEqual(snapshot_result["commit"], self.conn.execute(
            "SELECT target FROM operations WHERE kind='snapshot_commit' AND status='APPLIED' "
            "AND idempotency_id GLOB ?",
            (f"snap:{rnd['round_id']}:*",),
        ).fetchone()[0])

        submit_changes(self.conn, round_id=rnd["round_id"], actor="codex", commit=snapshot_result["commit"],
                       client_request_id="snapshot-claim")
        claimed_equal_tip = reconcile(self.conn, "snapshot")
        self.assertEqual(claimed_equal_tip["unattributed_commits"], 0)

        manual_tip = commit_file(wt, "manual.txt", "manual\n", "manual after snapshot")
        after_manual = reconcile(self.conn, "snapshot")
        self.assertEqual(after_manual["unattributed_commits"], 1)
        event = self.conn.execute("SELECT detail_json FROM events WHERE entity_id=? AND to_state='unattributed_commit'",
                                  (rnd["round_id"],)).fetchone()
        detail = json.loads(event[0])
        self.assertEqual(detail, {"expected": snapshot_result["commit"], "tip": manual_tip})
        self.assertEqual(reconcile(self.conn, "snapshot")["unattributed_commits"], 0)


if __name__ == "__main__":
    unittest.main()
