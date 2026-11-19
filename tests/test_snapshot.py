import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops.db import connect, migrate
from labops.errors import Blocked, UsageError, ValidationError
from labops.gitio import commit_trailers, rev_parse, run as git_run, show_file, status_porcelain
from labops.tasks import add_repo, create_task, create_worktree, open_round, snapshot
from tests.helpers import ROOT, make_repo


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "toy"
        make_repo(self.repo, files={"calc.py": "def add(a,b):\n    return a+b\n", "keep.txt": "base\n"})
        self.env_patch = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.env_patch.start()
        self.conn = connect(self.home / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo, worktree_root=self.root / "worktrees")
        self.task_id = "T20261019-snapshot"
        create_task(self.conn, repo_id="toy", task_id=self.task_id, goal="snapshot", profile={"schema": 1})
        self.round = open_round(self.conn, task_id=self.task_id, actor="codex", role="implement")
        self.wt = Path(create_worktree(self.conn, self.round["round_id"])["path"])

    def tearDown(self):
        self.conn.close()
        self.env_patch.stop()
        self.temp.cleanup()

    def put(self, path: str, content: bytes | str):
        target = self.wt.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return target

    def run_snapshot(self, files, message="snapshot"):
        return snapshot(self.conn, round_id=self.round["round_id"], files=files, message=message, actor="codex")

    def test_literal_add_trailers_and_idempotent_replay(self):
        self.put("-special.txt", "saved\n")
        result = self.run_snapshot(["-special.txt"], "save special")
        self.assertFalse(result["replayed"])
        self.assertEqual(rev_parse(self.wt, "HEAD"), result["commit"])
        trailers = commit_trailers(self.wt, result["commit"])
        self.assertEqual(trailers["LabOps-Task"], self.task_id)
        self.assertEqual(trailers["LabOps-Round"], "1")
        self.assertEqual(trailers["LabOps-Actor"], "codex")
        replay = self.run_snapshot(["-special.txt"], "save special")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["commit"], result["commit"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM operations WHERE kind='snapshot_commit'").fetchone()[0], 1)

    def test_deleted_file_is_snapshot_committed(self):
        (self.wt / "keep.txt").unlink()
        result = self.run_snapshot(["keep.txt"], "delete keep file")
        self.assertFalse(result["replayed"])
        self.assertIsNone(show_file(self.repo, result["commit"], "keep.txt"))
        self.assertNotIn("keep.txt", {path for _, path in status_porcelain(self.wt)})

    def test_concurrent_same_snapshot_serializes_at_repo_lock(self):
        self.put("keep.txt", "concurrent\n")
        env = os.environ.copy()
        env["LABOPS_HOME"] = str(self.home)
        env["PYTHONPATH"] = str(ROOT / "src")
        command = [sys.executable, "-m", "labops.cli", "snapshot", "--round", self.round["round_id"],
                   "--actor", "codex", "--message", "same concurrent snapshot", "--files", "keep.txt"]
        procs = [subprocess.Popen(command, cwd=self.repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(2)]
        outputs = [proc.communicate(timeout=30) for proc in procs]
        codes = [proc.returncode for proc in procs]
        self.assertEqual(codes, [0, 0], [stderr.decode("utf-8", "replace") for _, stderr in outputs])
        results = [json.loads(stdout.decode("utf-8")) for stdout, _ in outputs]
        self.assertEqual(results[0]["commit"], results[1]["commit"])
        self.assertEqual(sum(bool(result["replayed"]) for result in results), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM operations WHERE kind='snapshot_commit'").fetchone()[0], 1)

    def test_dirty_file_set_must_match_exactly(self):
        self.put("selected.txt", "selected")
        self.put("unlisted.txt", "unlisted")
        with self.assertRaisesRegex(Blocked, "unlisted_dirty"):
            self.run_snapshot(["selected.txt"])
        with self.assertRaisesRegex(ValidationError, "not_modified"):
            self.run_snapshot(["not-there.txt"])

    def test_path_boundary_deny_list_size_and_secret_guards(self):
        for path in ("../outside.txt", "C:\\outside.txt", "/absolute.txt"):
            with self.assertRaises(UsageError):
                self.run_snapshot([path])
        self.put("data/model.txt", "payload")
        with self.assertRaisesRegex(Blocked, "snapshot_guard_blocked"):
            self.run_snapshot(["data/model.txt"])
        (self.wt / "data" / "model.txt").unlink()
        token = "sk-" + "A" * 24
        self.put("secret.txt", token)
        with self.assertRaises(Blocked) as caught:
            self.run_snapshot(["secret.txt"])
        detail = json.dumps(caught.exception.detail)
        self.assertIn("secret.txt", detail)
        self.assertIn("openai_key", detail)
        self.assertNotIn(token, detail)
        (self.wt / "secret.txt").unlink()
        self.put("large.txt", b"x" * (5 * 1024 * 1024 + 1))
        with self.assertRaises(Blocked) as caught:
            self.run_snapshot(["large.txt"])
        self.assertIn("file_too_large", json.dumps(caught.exception.detail))

    def test_index_lock_blocks_without_deleting_lock(self):
        lock = Path(git_run(self.wt, "rev-parse", "--git-path", "index.lock").stdout.decode("utf-8").strip())
        if not lock.is_absolute():
            lock = self.wt / lock
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_bytes(b"keep")
        self.put("safe.txt", "safe")
        try:
            with self.assertRaises(Blocked) as caught:
                self.run_snapshot(["safe.txt"])
            self.assertEqual(caught.exception.code, "blocked")
            self.assertIn("index_lock_present", json.dumps(caught.exception.detail))
            self.assertEqual(lock.read_bytes(), b"keep")
        finally:
            lock.unlink()

    def test_files_must_be_nonempty_and_actor_must_match(self):
        with self.assertRaises(UsageError):
            self.run_snapshot([])
        with self.assertRaises(ValidationError):
            snapshot(self.conn, round_id=self.round["round_id"], files=["x"], message="x", actor="opus")


if __name__ == "__main__":
    unittest.main()
