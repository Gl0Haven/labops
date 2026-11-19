import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops import cli, gitio
from labops.canon import canonical_json
from labops.config import now
from labops.db import connect, migrate
from labops.evaluate import evaluate_retrieval
from labops.gate import client_restart_requirement
from labops.ids import new_id
from labops.index import _doc, index_stats, rebuild_index
from labops.legacy import import_legacy
from labops.onboard import onboard
from labops.search import _fts_doc_ids, _query_bigram_tokens, _quoted_fts, bigram_text, search
from labops.tasks import add_repo, create_task, open_round, set_confidential, set_repo_options
from tests.helpers import commit_file, git, make_repo


def _profile():
    return {"schema": 1, "commands": [], "requires_gpu": False,
            "allowed_paths": ["**/*"], "max_log_bytes": 1024}


def _make_master_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "-c", "init.defaultBranch=master", "init"],
                   capture_output=True, check=True)
    git(path, "config", "user.name", "labops-test")
    git(path, "config", "user.email", "t@example.invalid")
    git(path, "config", "core.autocrlf", "false")
    (path / "README.txt").write_text("master branch toy\n", encoding="utf-8")
    git(path, "add", "--all")
    git(path, "commit", "-m", "initial master")
    return git(path, "rev-parse", "HEAD").stdout.decode().strip()


class M2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-m2-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.env = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.env.start()
        self.conn = connect(self.home / "labops.db")
        migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def _seed_search_repo(self, *, confidential=True):
        repo = self.root / "search-repo"
        base = make_repo(repo, files={"README.txt": "temporary search repository\n"})
        add_repo(self.conn, repo_id="toy", path=repo)
        if confidential:
            set_confidential(self.conn, "toy", True)
        set_repo_options(self.conn, "toy", legacy_bridge=".ai-bridge")
        bridge = repo / ".ai-bridge"
        (bridge / "review").mkdir(parents=True)
        (bridge / "agent-status.md").write_text(
            f"owner: opus\nstate: review\ntask: T20261020-search-task\nround: 1\n"
            f"updated: {now()}\nbaseline: {base}\nspec: private/spec.md\n\n",
            encoding="utf-8")
        (bridge / "decisions.md").write_text(
            "## D-20261020-1 positional encoding\n位置编码 can preserve token positions.\n"
            "## D-20261020-1 duplicate encoding record\n另一段编码说明同一个编号。\n"
            "## D-20261020-2 cache design\nThe KV cache stores key and value tensors.\n"
            "## D-20261020-3 superseding update\n取代旧版 cache 方案，并加入错误回退路径。\n",
            encoding="utf-8")
        (bridge / "review" / "T20261020-search-task.md").write_text(
            "# search review title\nprivate review body\n", encoding="utf-8")
        archive = bridge / "review" / "plan-archive-m2.md"
        archive.write_text("# Archived plan\n\n## archiveonlytoken\nold plan text\n", encoding="utf-8")
        imported = import_legacy(self.conn, "toy")
        task_id = "T20261020-search-task"
        create_task(self.conn, repo_id="toy", task_id=task_id, goal="task goal for exact task lookup",
                    profile=_profile(), base=base)
        rnd = open_round(self.conn, task_id=task_id, actor="codex", role="implement")
        self._insert_superseded_finding(task_id, rnd["round_id"], base)
        return repo, base, archive, task_id, imported

    def _insert_superseded_finding(self, task_id: str, round_id: str, base: str) -> str:
        review_id = new_id("rv")
        pass_id = new_id("rv")
        tree = gitio.tree_of(Path(self.conn.execute("SELECT path FROM repos WHERE repo_id='toy'").fetchone()[0]), base)
        columns = ("review_id,task_id,round_id,reviewer,state,verdict,independence,base_commit,reviewed_commit,"
                   "reviewed_tree,scope_id,acceptance_profile_hash,findings_json,created_at,updated_at")
        finding = [{"id": "F-OLD", "severity": "major", "path": "toy.py", "text": "旧式 cache 失效策略"}]
        self.conn.execute(
            f"INSERT INTO reviews({columns}) VALUES(?,?,?,'opus','REWORK_REQUIRED','REWORK_REQUIRED','INDEPENDENT',?,?,?,?,?,?,?,?)",
            (review_id, task_id, round_id, base, base, tree, "scope", "profile",
             canonical_json(finding).decode(), "2026-10-20T01:00:00Z", "2026-10-20T01:00:00Z"),
        )
        self.conn.execute(
            f"INSERT INTO reviews({columns}) VALUES(?,?,?,'opus','PASS','PASS','INDEPENDENT',?,?,?,?,?,?,?,?)",
            (pass_id, task_id, round_id, base, base, tree, "scope", "profile", "[]",
             "2026-10-20T02:00:00Z", "2026-10-20T02:00:00Z"),
        )
        return pass_id

    def test_bigram_dual_search_exact_refs_supersession_redaction_and_incremental_index(self):
        self.assertEqual(bigram_text("位置编码 KV"), "位置 置编 编码 kv")
        self.assertEqual(bigram_text("编"), "编")
        self.assertEqual(bigram_text("A-1, KV!"), "a 1 kv")
        self.assertEqual(bigram_text("KV kv"), "kv kv")
        self.assertEqual(_query_bigram_tokens("A-1, KV!"), ["kv"])
        self.assertEqual(_query_bigram_tokens("KV kv"), ["kv"])
        self.assertEqual(_query_bigram_tokens("A 1 !!!"), [])
        long_query = " ".join(f"word{index}" for index in range(65))
        self.assertEqual(len(bigram_text(long_query).split()), 65)
        self.assertEqual(len(_query_bigram_tokens(long_query)), 64)
        repo, _base, archive, task_id, imported = self._seed_search_repo()
        self.assertEqual(imported["by_kind"]["decision"], 4)

        first = rebuild_index(self.conn, "toy")
        self.assertGreater(first["indexed"], 0)
        self.assertGreater(index_stats(self.conn)["documents"], 0)
        tri_hits = set(_fts_doc_ids(self.conn, "search_tri", _quoted_fts("位置编码")))
        d1_ids = {row[0] for row in self.conn.execute(
            "SELECT doc_id FROM search_docs WHERE repo_id='toy' AND kind='decision' AND ref='D-20261020-1'")}
        self.assertTrue(tri_hits & d1_ids)
        self.assertEqual(rebuild_index(self.conn, "toy")["updated"], 0)

        cjk = search(self.conn, "编码", repo_id="toy")
        self.assertTrue(any(item["ref"] == "D-20261020-1" for item in cjk["results"]), cjk)
        abbreviation = search(self.conn, "KV", repo_id="toy")
        self.assertTrue(any(item["ref"] == "D-20261020-2" for item in abbreviation["results"]), abbreviation)
        exact = search(self.conn, "D-20261020-1", repo_id="toy")
        self.assertEqual(sum(item["kind"] == "decision" for item in exact["results"]), 2)
        task_exact = search(self.conn, task_id, repo_id="toy")
        self.assertTrue(any(item["kind"] == "task" and item["ref"] == task_id for item in task_exact["results"]))

        superseded = search(self.conn, "旧式 cache", repo_id="toy")
        self.assertFalse(any(item["kind"] == "finding" for item in superseded["results"]))
        included = search(self.conn, "旧式 cache", repo_id="toy", include_superseded=True)
        old_finding = next(item for item in included["results"] if item["kind"] == "finding")
        self.assertFalse(old_finding["valid"])
        self.assertTrue(old_finding["ref"].endswith("#F-OLD"))

        secretary = search(self.conn, "编码", repo_id="toy", actor="secretary")
        self.assertTrue(secretary["results"])
        self.assertTrue(all(set(item) == {"repo_id", "kind", "ref", "ts"} for item in secretary["results"]))
        self.assertFalse(any("private/spec.md" in json.dumps(item) for item in secretary["results"]))

        archive.unlink()
        deleted = rebuild_index(self.conn, "toy")
        self.assertGreaterEqual(deleted["deleted"], 2)
        self.assertEqual(search(self.conn, "archiveonlytoken", repo_id="toy")["count"], 0)

    def test_or_recall_uses_phrase_fragments_and_preserves_exact_id_priority(self):
        self._seed_search_repo(confidential=False)
        rebuild_index(self.conn, "toy")
        with patch("labops.search._fts_doc_ids", wraps=_fts_doc_ids) as fts_query:
            partial = search(self.conn, "cache, nonexistenttoken", repo_id="toy")
        self.assertTrue(any(item["ref"] == "D-20261020-2" for item in partial["results"]), partial)
        matches = [call.args[2] for call in fts_query.call_args_list]
        self.assertIn('"cache" OR "nonexistenttoken"', matches)
        self.assertEqual(len(matches), 3)

        exact = search(self.conn, "D-20261020-1", repo_id="toy")
        self.assertEqual([item["ref"] for item in exact["results"][:2]],
                         ["D-20261020-1", "D-20261020-1"])
        self.assertTrue(all(item["score"] == 1_000_000_000.0 for item in exact["results"][:2]))

    def test_long_index_body_tail_remains_in_bigram_index_with_original_content_hash(self):
        repo, _base, _archive, _task_id, _imported = self._seed_search_repo(confidential=False)
        body = " ".join([*(f"prefixword{index}" for index in range(200)), "longtailneedle"])
        review_path = repo / ".ai-bridge" / "review" / "long.md"
        review_path.write_text(f"# Long index fixture\n## Long content\n{body}\n", encoding="utf-8")
        rebuild_index(self.conn, "toy")
        row = self.conn.execute(
            "SELECT doc_id,body,content_hash FROM search_docs WHERE repo_id='toy' "
            "AND kind='review_section' AND ref='long.md#1'"
        ).fetchone()
        self.assertEqual(row["body"], body)
        self.assertIn(row["doc_id"], _fts_doc_ids(self.conn, "search_bi", _quoted_fts("longtailneedle")))
        expected = _doc("toy", "review_section", "long.md#1", path=".ai-bridge/review/long.md", line=2,
                        title="Long content", body=body, ts=None, confidential=False)
        self.assertEqual(row["content_hash"], expected["content_hash"])
        self.assertEqual(row["content_hash"], "79dd0be223e3ad6990f679a0ab9afacce83f328c76d334da83a4d11115e66bfa")

    def test_legacy_cli_counts_by_kind_and_verbose_records(self):
        self._seed_search_repo(confidential=False)
        counts = cli.dispatch(cli.build_parser().parse_args(["legacy", "import", "--repo-id", "toy"]))
        self.assertEqual(set(counts), {"imported", "by_kind"})
        self.assertEqual(counts["by_kind"]["decision"], 4)
        verbose = cli.dispatch(cli.build_parser().parse_args(
            ["legacy", "import", "--repo-id", "toy", "--verbose"]))
        self.assertEqual(len(verbose["records"]), counts["imported"])
        audit = cli.dispatch(cli.build_parser().parse_args(["legacy", "audit", "--repo-id", "toy"]))
        self.assertEqual(set(audit), {"imported", "by_kind"})
        audit_verbose = cli.dispatch(cli.build_parser().parse_args(
            ["legacy", "audit", "--repo-id", "toy", "--verbose"]))
        self.assertIn("records", audit_verbose)
        self.assertIn("detected", audit_verbose)

    def test_onboard_master_branch_is_safe_and_resumes_idempotently(self):
        repo = self.root / "master-repo"
        base = _make_master_repo(repo)
        bridge = repo / ".ai-bridge"
        (bridge / "review").mkdir(parents=True)
        (bridge / "agent-status.md").write_text(
            f"owner: opus\nstate: review\ntask: T20261020-onboard-toy\nround: 1\n"
            f"updated: {now()}\nbaseline: {base}\n\n", encoding="utf-8")
        (bridge / "decisions.md").write_text("## D-20261020-7 master branch onboarding\nUTF smoke\n", encoding="utf-8")
        (bridge / "current-plan.md").write_text("# Toy plan\n\n## onboardmarker\nmaster path\n", encoding="utf-8")
        options = ["onboard", "--repo-id", "toy-master", "--path", str(repo), "--main-ref", "auto",
                   "--confidential", "--legacy-bridge", ".ai-bridge",
                   "--backup-root", str(self.root / "backups"), "--index"]
        first = cli.dispatch(cli.build_parser().parse_args(options))
        self.assertEqual(first["main_ref"], "refs/heads/master")
        self.assertTrue(first["steps"]["register"]["registered"])
        self.assertTrue(first["steps"]["backup"]["verify_restore"]["ok"])
        self.assertGreater(first["steps"]["index"]["indexed"], 0)
        row = self.conn.execute("SELECT trusted_for_exec,python,confidential,main_ref FROM repos WHERE repo_id='toy-master'").fetchone()
        self.assertEqual(tuple(row), (0, None, 1, "refs/heads/master"))
        event_count = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        backup_count = self.conn.execute("SELECT COUNT(*) FROM backups WHERE repo_id='toy-master'").fetchone()[0]

        second = cli.dispatch(cli.build_parser().parse_args(options))
        self.assertFalse(second["steps"]["register"]["registered"])
        self.assertTrue(second["steps"]["backup"]["skipped"])
        self.assertEqual(second["steps"]["index"]["updated"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], event_count)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM backups WHERE repo_id='toy-master'").fetchone()[0], backup_count)

    def test_restart_flag_compares_server_and_worker_tool_registry(self):
        repo = self.root / "restart-repo"
        base = make_repo(repo, files={
            "src/labops/mcp_server.py": "def tool(value):\n    return value\n",
            "src/labops/mcp_worker.py": "TOOLS = {'get': ('value',)}\n\ndef impl():\n    return 1\n",
        })
        implementation_only = commit_file(repo, "src/labops/mcp_worker.py",
                                           "TOOLS = {'get': ('value',)}\n\ndef impl():\n    return 2\n")
        self.assertEqual(client_restart_requirement(repo, base, implementation_only),
                         {"client_restart_required": False, "reason": "worker_implementation_only"})
        signature_changed = commit_file(repo, "src/labops/mcp_worker.py",
                                        "TOOLS = {'get': ('value',), 'search_history': ('query',)}\n")
        self.assertEqual(client_restart_requirement(repo, implementation_only, signature_changed),
                         {"client_restart_required": True, "reason": "tool_registry_changed"})
        server_changed = commit_file(repo, "src/labops/mcp_server.py", "def tool(value, extra=None):\n    return value\n")
        self.assertEqual(client_restart_requirement(repo, signature_changed, server_changed),
                         {"client_restart_required": True, "reason": "mcp_server_changed"})

    def test_search_and_eval_cli_write_reports_under_requested_temp_directory(self):
        repo, _base, _archive, _task_id, _imported = self._seed_search_repo(confidential=False)
        rebuilt = cli.dispatch(cli.build_parser().parse_args(["index", "rebuild", "--repo-id", "toy"]))
        self.assertGreater(rebuilt["total"], 0)
        stats = cli.dispatch(cli.build_parser().parse_args(["index", "stats"]))
        self.assertGreater(stats["documents"], 0)
        searched = cli.dispatch(cli.build_parser().parse_args(["search", "编码", "--repo-id", "toy"]))
        self.assertTrue(searched["results"])
        eval_file = self.root / "questions.jsonl"
        eval_file.write_text(json.dumps({"id": "q1", "query": "编码", "expected": [
            {"repo_id": "toy", "ref": "D-20261020-1"}], "tags": ["two_char"]}) + "\n", encoding="utf-8")
        output = self.root / "eval-out"
        evaluated = cli.dispatch(cli.build_parser().parse_args(
            ["eval", "retrieval", "--file", str(eval_file), "--k", "1,5", "--out", str(output)]))
        self.assertEqual(evaluated["report"]["questions"], 1)
        self.assertEqual(evaluated["report"]["metrics"]["recall@5"], 1.0)
        self.assertTrue(Path(evaluated["json_path"]).is_file())
        self.assertTrue(Path(evaluated["markdown_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
