import json
import os
import sys
import tempfile
import unittest
import venv
from pathlib import Path
from unittest.mock import patch

from labops import cli, gitio
from labops.backup import run_backup, verify_restore
from labops.db import connect, migrate
from labops.errors import Blocked, ValidationError
from labops.history import query_history
from labops.legacy import audit_legacy, import_legacy
from labops.notify import scan
from labops.progress import progress_summary
from labops.redact import for_actor
from labops.tasks import (abandon_task, add_repo, create_task, create_worktree, list_repos,
                          open_round, prune_worktrees, set_confidential, set_repo_options)
from labops.verify import verify_round
from tests.helpers import commit_file, git, make_repo


def _profile(commands=None):
    return {"schema": 1, "commands": commands or [], "requires_gpu": False,
            "allowed_paths": ["**/*"], "max_log_bytes": 2048}


class M1FeaturesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-m1-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.base = make_repo(self.repo, files={"README.txt": "toy\n", "tracked.txt": "base\n"})
        self.home = self.root / "home"
        self.env = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.env.start()
        self.conn = connect(self.home / "labops.db")
        migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def add_default_repo(self, *, repo_id="toy", trusted=True, profile=None, worktree_root=None):
        add_repo(self.conn, repo_id=repo_id, path=self.repo, trusted_for_exec=trusted,
                 worktree_root=worktree_root)
        task_id = f"T20261020-{repo_id}-task"
        create_task(self.conn, repo_id=repo_id, task_id=task_id, goal="toy task", profile=profile or _profile())
        rnd = open_round(self.conn, task_id=task_id, actor="codex", role="implement")
        submitted = commit_file(self.repo, "submitted.txt", f"{repo_id}\n")
        from labops.tasks import submit_changes
        submit_changes(self.conn, round_id=rnd["round_id"], actor="codex", commit=submitted,
                       client_request_id=f"submit-{repo_id}")
        return task_id, rnd["round_id"]

    def test_repo_cli_settings_require_python_confirmation_and_list(self):
        add_repo(self.conn, repo_id="toy", path=self.repo)
        second_repo = self.root / "second-repo"
        make_repo(second_repo)
        args = cli.build_parser().parse_args(["repo", "add", "--repo-id", "toy2", "--path", str(second_repo),
                                              "--confidential"])
        added = cli.dispatch(args)
        self.assertTrue(added["confidential"])
        python_path = str(Path(sys.executable).resolve())
        args = cli.build_parser().parse_args(["repo", "set", "--repo-id", "toy", "--python", python_path,
                                              "--backup-root", str(self.root / "backups"), "--backup-keep", "2",
                                              "--legacy-bridge", ".ai-bridge"])
        with patch("labops.cli.require_human", return_value="tty") as confirm:
            configured = cli.dispatch(args)
        confirm.assert_called_once()
        self.assertEqual(configured["python"], str(Path(python_path).resolve()))
        self.assertEqual(configured["backup_keep"], 2)
        self.assertEqual(configured["legacy_bridge"], ".ai-bridge")
        self.assertEqual({row["repo_id"] for row in list_repos(self.conn)}, {"toy", "toy2"})
        with self.assertRaisesRegex(ValidationError, "backup_root_inside_repo"):
            set_repo_options(self.conn, "toy", backup_root=str(self.repo / "backups"))

    def test_configured_python_is_used_and_missing_python_blocks(self):
        task_id, round_id = self.add_default_repo(profile=_profile([
            {"name": "python", "argv": ["{python}", "-c", "import sys; print(sys.executable)"],
             "cwd": ".", "timeout_s": 30}
        ]))
        fake_env = self.root / "repo-python"
        venv.EnvBuilder(with_pip=False).create(fake_env)
        python_path = fake_env / "Scripts" / "python.exe"
        set_repo_options(self.conn, "toy", python=str(python_path))
        result = verify_round(self.conn, round_id)
        self.assertEqual(result["state"], "VERIFIED")
        evidence = self.conn.execute("SELECT env_json FROM evidence WHERE evidence_id=?", (result["evidence_id"],)).fetchone()
        env_info = json.loads(evidence["env_json"])
        self.assertEqual(Path(env_info["python_resolved"]).resolve(), python_path.resolve())
        self.assertTrue(env_info["python_version"].startswith("Python "))
        output = Path(result["results"][0]["log_path"]).read_text(encoding="utf-8")
        self.assertEqual(Path(output.strip()).resolve(), python_path.resolve())

        next_round = open_round(self.conn, task_id=task_id, actor="codex", role="implement")
        new_head = commit_file(self.repo, "submitted2.txt", "second\n")
        from labops.tasks import submit_changes
        submit_changes(self.conn, round_id=next_round["round_id"], actor="codex", commit=new_head,
                       client_request_id="submit-to-missing-python")
        missing = self.root / "missing-python.exe"
        set_repo_options(self.conn, "toy", python=str(missing))
        with self.assertRaisesRegex(Blocked, "repo_python_missing"):
            verify_round(self.conn, next_round["round_id"])
        blocked_env = json.loads(self.conn.execute(
            "SELECT env_json FROM evidence WHERE round_id=? AND kind='INDEPENDENTLY_EXECUTED'",
            (next_round["round_id"],),
        ).fetchone()[0])
        self.assertEqual(blocked_env["python_resolved"], str(missing.resolve()))
        self.assertIsNone(blocked_env["python_version"])

    def test_bundle_backup_restore_rotation_and_registered_file_boundary(self):
        add_repo(self.conn, repo_id="toy", path=self.repo)
        backup_root = self.root / "backups"
        set_repo_options(self.conn, "toy", backup_root=str(backup_root), backup_keep=2)
        git(self.repo, "update-ref", "refs/labops/toy/r1/codex/submitted", self.base)
        directory = backup_root / "toy"
        directory.mkdir(parents=True)
        unregistered = directory / "unregistered.bundle"
        unregistered.write_text("leave me", encoding="utf-8")

        first = run_backup(self.conn, "toy")
        second = run_backup(self.conn, "toy")
        third = run_backup(self.conn, "toy")
        self.assertTrue(first["verified"] and second["verified"] and third["verified"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM backups WHERE repo_id='toy'").fetchone()[0], 2)
        self.assertTrue(unregistered.exists())
        self.assertFalse(Path(first["path"]).exists())
        restored = verify_restore(self.conn, "toy")
        self.assertTrue(restored["ok"], restored)
        self.assertEqual(restored["missing"], [])
        self.assertEqual(restored["mismatched"], [])

    def test_legacy_import_audit_redaction_history_and_read_only_files(self):
        add_repo(self.conn, repo_id="toy", path=self.repo)
        set_confidential(self.conn, "toy", True)
        set_repo_options(self.conn, "toy", legacy_bridge=".ai-bridge")
        bridge = self.repo / ".ai-bridge"
        review = bridge / "review" / "T20261020-legacy-task.md"
        review.parent.mkdir(parents=True)
        status = (
            "owner: opus\nstate: review\ntask: T20261020-legacy-task\nround: 2\n"
            "updated: 2020-01-01T00:00:00Z\nbaseline: " + self.base + "\nspec: private/spec.md\n\n"
            "# body secret that must not be imported\n"
        )
        (bridge / "agent-status.md").write_text(status, encoding="utf-8")
        (bridge / "decisions.md").write_text(
            "## D-20261020-4 frozen title\nprivate decision body\n"
            "## D-20261020-4 duplicate title\nsecond private body\n", encoding="utf-8")
        review.write_text("# private review title\nprivate review body\n", encoding="utf-8")
        before = {path.relative_to(bridge).as_posix(): (path.stat().st_mtime_ns, path.read_bytes())
                  for path in bridge.rglob("*") if path.is_file()}

        imported = import_legacy(self.conn, "toy")
        self.assertEqual(imported["imported"], 10)
        duplicate_decisions = self.conn.execute(
            "SELECT key,ref FROM legacy_records WHERE repo_id='toy' AND kind='decision' ORDER BY line"
        ).fetchall()
        self.assertEqual(len(duplicate_decisions), 2)
        self.assertEqual({row["ref"] for row in duplicate_decisions}, {"D-20261020-4"})
        self.assertNotEqual(duplicate_decisions[0]["key"], duplicate_decisions[1]["key"])
        after_import = {path.relative_to(bridge).as_posix(): (path.stat().st_mtime_ns, path.read_bytes())
                        for path in bridge.rglob("*") if path.is_file()}
        self.assertEqual(after_import, before)
        self.assertEqual(self.conn.execute(
            "SELECT value FROM legacy_records WHERE repo_id='toy' AND kind='status' AND key='baseline'"
        ).fetchone()[0], self.base)

        commit_file(self.repo, "tracked.txt", "advanced\n", message="advance after legacy baseline")
        (self.repo / "README.txt").write_text("dirty tracked change\n", encoding="utf-8")
        first_audit = audit_legacy(self.conn, "toy")
        self.assertEqual({item["kind"] for item in first_audit["detected"]},
                         {"behind", "dirty_worktree", "stale_updated"})
        self.assertEqual(audit_legacy(self.conn, "toy")["detected"], [])
        events = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE entity_type='repo' AND entity_id='toy' AND to_state='legacy_drift'"
        ).fetchone()[0]
        self.assertEqual(events, 3)

        public_summary = progress_summary(self.conn)
        legacy_item = public_summary["legacy"][0]
        self.assertEqual(legacy_item["state"], "review")
        self.assertNotIn("baseline", legacy_item)
        secretary_summary = for_actor(self.conn, "secretary", public_summary)
        self.assertNotIn("private/spec.md", json.dumps(secretary_summary))
        self.assertNotIn(self.base, json.dumps(secretary_summary))
        history = query_history(self.conn, task_id="T20261020-legacy-task")
        self.assertEqual({item["kind"] for item in history["legacy"]}, {"review", "decision"})
        secretary_history = for_actor(self.conn, "secretary", history)
        self.assertNotIn("private review title", json.dumps(secretary_history))
        self.assertNotIn(".ai-bridge", json.dumps(secretary_history))

        after_audit = {path.relative_to(bridge).as_posix(): (path.stat().st_mtime_ns, path.read_bytes())
                       for path in bridge.rglob("*") if path.is_file()}
        self.assertEqual(after_audit, before)
        self.assertEqual(scan(self.conn), 3)
        notifications = self.conn.execute(
            "SELECT kind,text FROM notifications WHERE kind='legacy_drift' ORDER BY event_key"
        ).fetchall()
        self.assertEqual(len(notifications), 3)
        self.assertTrue(all("[LabOps · toy] 旧流程 · 旧流程状态漂移" in row["text"] for row in notifications))

    def test_prune_worktrees_dry_run_apply_branch_preservation_and_dirty_skip(self):
        worktree_root = self.root / "worktrees"
        add_repo(self.conn, repo_id="toy", path=self.repo, worktree_root=worktree_root)
        task_id = "T20261020-prune-clean"
        create_task(self.conn, repo_id="toy", task_id=task_id, goal="prune", profile=_profile())
        rnd = open_round(self.conn, task_id=task_id, actor="codex", role="implement")
        clean = create_worktree(self.conn, rnd["round_id"])
        labops_ref = "refs/labops/prune/keep"
        git(self.repo, "update-ref", labops_ref, self.base)
        abandon_task(self.conn, task_id)
        dry_run = prune_worktrees(self.conn, task_id)
        self.assertEqual(dry_run["worktrees"][0]["status"], "would_remove")
        self.assertTrue(Path(clean["path"]).is_dir())
        self.assertEqual(prune_worktrees(self.conn, task_id, apply=True)["worktrees"][0]["status"], "removed")
        self.assertFalse(Path(clean["path"]).exists())
        self.assertIsNotNone(gitio.read_ref(self.repo, f"refs/heads/{clean['branch']}"))
        self.assertEqual(gitio.read_ref(self.repo, labops_ref), self.base)

        dirty_task = "T20261020-prune-dirty"
        create_task(self.conn, repo_id="toy", task_id=dirty_task, goal="prune dirty", profile=_profile())
        dirty_round = open_round(self.conn, task_id=dirty_task, actor="codex", role="implement")
        dirty = create_worktree(self.conn, dirty_round["round_id"])
        (Path(dirty["path"]) / "untracked.txt").write_text("keep", encoding="utf-8")
        abandon_task(self.conn, dirty_task)
        result = prune_worktrees(self.conn, dirty_task, apply=True)
        self.assertEqual(result["worktrees"][0]["reason"], "worktree_dirty")
        self.assertTrue(Path(dirty["path"]).is_dir())


if __name__ == "__main__":
    unittest.main()
