import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops import cli
from labops.db import connect, migrate
from labops.index import rebuild_index
from labops.next_task import next_task
from labops.search import search
from labops.tasks import (add_repo, create_task, create_worktree, get_handoff_rules,
                          open_round, set_confidential)
from tests.helpers import make_repo


class HandoffExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-handoff-experience-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        make_repo(self.repo)
        self.env = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.env.start()
        self.conn = connect(self.home / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo,
                 worktree_root=self.root / "worktrees", trusted_for_exec=True)
        self.task_id = "T20261020-handoff"
        create_task(self.conn, repo_id="toy", task_id=self.task_id, goal="handoff task",
                    profile={"schema": 1}, brief="initial brief")

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def test_cli_sets_brief_rules_and_mute(self):
        brief_path = self.root / "brief.md"
        brief_path.write_text("CLI-created brief\n", encoding="utf-8")
        profile_path = self.root / "profile.json"
        profile_path.write_text(json.dumps({"schema": 1}), encoding="utf-8")
        new_brief_path = self.root / "new-brief.md"
        new_brief_path.write_text("Updated by CLI\n", encoding="utf-8")
        rules_path = self.root / "rules.md"
        rules_path.write_text("- One shared rule\n", encoding="utf-8")

        created = cli.dispatch(cli.build_parser().parse_args([
            "task", "create", "--repo-id", "toy", "--task-id", "T20261020-cli-brief",
            "--goal", "CLI brief", "--profile", str(profile_path), "--brief-file", str(brief_path),
        ]))
        self.assertEqual(created["state"], "CREATED")
        self.assertEqual(self.conn.execute("SELECT brief FROM tasks WHERE task_id='T20261020-cli-brief'").fetchone()[0],
                         "CLI-created brief\n")
        updated = cli.dispatch(cli.build_parser().parse_args([
            "task", "brief", "--task-id", "T20261020-cli-brief", "--set-file", str(new_brief_path),
        ]))
        self.assertTrue(updated["changed"])
        self.assertEqual(self.conn.execute("SELECT brief FROM tasks WHERE task_id='T20261020-cli-brief'").fetchone()[0],
                         "Updated by CLI\n")
        rules = cli.dispatch(cli.build_parser().parse_args([
            "handoff", "rules", "--set-file", str(rules_path),
        ]))
        self.assertTrue(rules["changed"])
        self.assertEqual(get_handoff_rules(self.conn), "- One shared rule\n")

        muted = cli.dispatch(cli.build_parser().parse_args(["notify", "mute", "review_passed"]))
        self.assertTrue(muted["muted"])
        next_result = cli.dispatch(cli.build_parser().parse_args(["next", "--actor", "codex"]))
        self.assertEqual(next_result["action"], "start_new")
        self.assertIn("handoff", next_result)

    def test_next_task_codex_priority_and_untrusted_filter(self):
        fresh = next_task(self.conn, "codex")
        self.assertEqual(fresh["action"], "start_new")
        self.assertEqual(fresh["task_id"], self.task_id)
        self.assertEqual(fresh["context_ref"], self.task_id)
        self.assertEqual(fresh["handoff"]["brief"], "initial brief")

        rnd = open_round(self.conn, task_id=self.task_id, actor="codex", role="implement")
        worktree = create_worktree(self.conn, rnd["round_id"])
        continuing = next_task(self.conn, "codex")
        self.assertEqual(continuing["action"], "continue_round")
        self.assertEqual(continuing["round_id"], rnd["round_id"])
        self.assertEqual(Path(continuing["worktree_path"]), Path(worktree["path"]))

        self.conn.execute("UPDATE rounds SET state='REJECTED' WHERE round_id=?", (rnd["round_id"],))
        rework = next_task(self.conn, "codex")
        self.assertEqual(rework["action"], "start_rework")
        self.assertEqual(rework["task_id"], self.task_id)
        self.assertIn("findings", rework["open_findings"])

        untrusted_repo = self.root / "untrusted"
        make_repo(untrusted_repo)
        add_repo(self.conn, repo_id="untrusted", path=untrusted_repo,
                 worktree_root=self.root / "untrusted-worktrees", trusted_for_exec=False)
        create_task(self.conn, repo_id="untrusted", task_id="T20261020-untrusted",
                    goal="must not be dispatched", profile={"schema": 1})
        self.conn.execute("UPDATE tasks SET state='ABANDONED' WHERE task_id=?", (self.task_id,))
        self.assertEqual(next_task(self.conn, "codex"), {"action": "idle"})

        labops_repo = self.root / "labops"
        make_repo(labops_repo)
        add_repo(self.conn, repo_id="labops", path=labops_repo,
                 worktree_root=self.root / "labops-worktrees", trusted_for_exec=False)
        create_task(self.conn, repo_id="labops", task_id="T20261020-labops-self",
                    goal="LabOps remains eligible", profile={"schema": 1})
        self.assertEqual(next_task(self.conn, "codex")["task_id"], "T20261020-labops-self")

    def test_next_task_uses_created_at_order_within_priority(self):
        create_task(self.conn, repo_id="toy", task_id="T20261020-handoff-older", goal="older task",
                    profile={"schema": 1})
        self.conn.execute("UPDATE tasks SET created_at='2026-01-01T00:00:00Z' WHERE task_id=?",
                          ("T20261020-handoff-older",))
        self.conn.execute("UPDATE tasks SET created_at='2026-02-01T00:00:00Z' WHERE task_id=?",
                          (self.task_id,))
        selected = next_task(self.conn, "codex")
        self.assertEqual(selected["action"], "start_new")
        self.assertEqual(selected["task_id"], "T20261020-handoff-older")

    def test_next_task_opus_review_verify_block_and_idle(self):
        self.assertEqual(next_task(self.conn, "opus"), {"action": "idle"})
        rnd = open_round(self.conn, task_id=self.task_id, actor="codex", role="implement")
        self.conn.execute("UPDATE rounds SET state='CLAIMED' WHERE round_id=?", (rnd["round_id"],))
        verify = next_task(self.conn, "opus")
        self.assertEqual(verify["action"], "verify")
        self.assertEqual(verify["hint"], f"labops verify --round {rnd['round_id']}")

        self.conn.execute("UPDATE rounds SET state='VERIFIED',head_commit=?,head_tree=? WHERE round_id=?",
                          ("a" * 40, "b" * 40, rnd["round_id"]))
        review = next_task(self.conn, "opus")
        self.assertEqual(review["action"], "review")
        self.assertEqual(review["round_id"], rnd["round_id"])

        self.conn.execute("UPDATE rounds SET state='BLOCKED' WHERE round_id=?", (rnd["round_id"],))
        blocked = next_task(self.conn, "opus")
        self.assertEqual(blocked["action"], "resolve_block")
        self.assertEqual(blocked["round_id"], rnd["round_id"])
        self.conn.execute("UPDATE rounds SET state='OPEN' WHERE round_id=?", (rnd["round_id"],))
        self.conn.execute("UPDATE tasks SET state='ABANDONED' WHERE task_id=?", (self.task_id,))
        self.assertEqual(next_task(self.conn, "opus"), {"action": "idle"})

    def test_opus_ignores_stale_verified_rounds_and_integrated_tasks(self):
        stale_task_id = "T20261020-handoff-stale"
        stale_task = create_task(self.conn, repo_id="toy", task_id=stale_task_id, goal="stale review",
                                 profile={"schema": 1})
        first = open_round(self.conn, task_id=stale_task_id, actor="codex", role="implement")
        first_head, first_tree = "a" * 40, "b" * 40
        self.conn.execute("UPDATE rounds SET state='VERIFIED',head_commit=?,head_tree=? WHERE round_id=?",
                          (first_head, first_tree, first["round_id"]))
        self.conn.execute(
            "INSERT INTO reviews(review_id,task_id,round_id,reviewer,state,verdict,independence,base_commit,reviewed_commit,reviewed_tree,scope_id,acceptance_profile_hash,findings_json,created_at,updated_at) "
            "VALUES('rv-stale-opus',?,?,'opus','STALE','PASS','INDEPENDENT',?,?,?,'scope',?,'[]','2026-10-20T00:00:00Z','2026-10-20T00:00:00Z')",
            (stale_task_id, first["round_id"], stale_task["base_commit"], first_head, first_tree,
             self.conn.execute("SELECT profile_hash FROM tasks WHERE task_id=?", (stale_task_id,)).fetchone()[0]),
        )
        open_round(self.conn, task_id=stale_task_id, actor="codex", role="implement")
        self.assertEqual(next_task(self.conn, "opus"), {"action": "idle"})

        terminal_task_id = "T20261020-handoff-integrated"
        create_task(self.conn, repo_id="toy", task_id=terminal_task_id, goal="integrated task",
                    profile={"schema": 1})
        terminal_round = open_round(self.conn, task_id=terminal_task_id, actor="codex", role="implement")
        self.conn.execute("UPDATE rounds SET state='VERIFIED',head_commit=?,head_tree=? WHERE round_id=?",
                          ("c" * 40, "d" * 40, terminal_round["round_id"]))
        self.conn.execute("UPDATE tasks SET state='INTEGRATED' WHERE task_id=?", (terminal_task_id,))
        self.conn.execute("UPDATE tasks SET state='ABANDONED' WHERE task_id=?", (stale_task_id,))
        self.assertEqual(next_task(self.conn, "opus"), {"action": "idle"})

    def test_review_and_research_sections_are_indexed_and_secretary_redacted(self):
        bridge = self.repo / ".ai-bridge"
        review_dir = bridge / "review"
        research_dir = bridge / "research"
        review_dir.mkdir(parents=True)
        research_dir.mkdir(parents=True)
        review_file = review_dir / "review.md"
        review_file.write_text(
            "# Review\n## First finding\nreviewsectionneedle appears here\n"
            "## Second finding\nsecondreviewneedle appears here\n" + "x" * 4500 + "\n", encoding="utf-8")
        research_file = research_dir / "source.md"
        research_file.write_text("## Research result\nresearchsectionneedle evidence\n", encoding="utf-8")
        large_file = review_dir / "large.md"
        large_file.write_bytes(b"x" * (2 * 1024 * 1024 + 1))

        result = rebuild_index(self.conn, "toy")
        self.assertEqual(result["skipped_large"], 1)
        self.assertEqual(result["skipped_files"][0]["path"], ".ai-bridge/review/large.md")
        review = search(self.conn, "reviewsectionneedle", repo_id="toy", kinds=["review_section"])
        self.assertEqual(review["results"][0]["ref"], "review.md#1")
        self.assertEqual(review["results"][0]["line"], 2)
        self.assertEqual(review["results"][0]["title"], "First finding")
        second_body = self.conn.execute(
            "SELECT body FROM search_docs WHERE repo_id='toy' AND kind='review_section' AND ref='review.md#2'"
        ).fetchone()[0]
        self.assertEqual(len(second_body), 4000)
        research = search(self.conn, "researchsectionneedle", repo_id="toy", kinds=["research_section"])
        self.assertEqual(research["results"][0]["ref"], "source.md#1")

        set_confidential(self.conn, "toy", True)
        rebuild_index(self.conn, "toy")
        secretary = search(self.conn, "reviewsectionneedle", repo_id="toy",
                           kinds=["review_section"], actor="secretary")
        self.assertEqual(set(secretary["results"][0]), {"repo_id", "kind", "ref", "title"})
        self.assertEqual(secretary["results"][0]["title"], "First finding")


if __name__ == "__main__":
    unittest.main()
