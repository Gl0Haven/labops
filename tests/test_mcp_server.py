import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops.db import connect, migrate
from labops.gate import gate_check
from labops.index import rebuild_index
from labops.tasks import add_repo, create_task, create_worktree, open_round, set_confidential
from labops.verify import verify_round
from tests.helpers import ROOT, commit_file, make_repo

RUNNABLE_PROFILE = {"schema": 1, "commands": [{"name": "unit", "argv": ["{python}", "-c", "pass"], "cwd": ".", "timeout_s": 60}]}

PYTHON = Path(os.environ.get("LABOPS_TEST_PYTHON") or sys.executable)
SRC = ROOT / "src"
MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None
if MCP_AVAILABLE:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.server.mcpserver.exceptions import ToolError
    from labops import mcp_server
else:
    ClientSession = StdioServerParameters = stdio_client = ToolError = mcp_server = None


class MCPServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-mcp-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.base = make_repo(self.repo)
        self.task_id = "T20261019-mcp"
        self.round_id = f"{self.task_id}/r1"
        self.home = self.root / "home"
        self._seed_home(self.home, self.repo, base=self.base)
        self.commit = commit_file(self.repo, "result.txt", "mcp result\n")
        self.env_patch = patch.dict(os.environ, {"LABOPS_HOME": str(self.home), "LABOPS_ACTOR": "codex"})
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.temp.cleanup()

    def _seed_home(self, home: Path, repo: Path, *, base: str | None = None):
        conn = connect(home / "labops.db")
        try:
            migrate(conn)
            add_repo(conn, repo_id="toy", path=repo)
            create_task(conn, repo_id="toy", task_id=self.task_id, goal="MCP path", profile=RUNNABLE_PROFILE, base=base)
            open_round(conn, task_id=self.task_id, actor="codex", role="implement")
        finally:
            conn.close()

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_direct_tool_calls_and_error_mapping(self):
        context = mcp_server.get_task_context(task_id=self.task_id)
        self.assertEqual(context["sections"]["task"]["task_id"], self.task_id)
        self.assertEqual(mcp_server.next_task(), {"action": "idle"})
        handoff = mcp_server.get_handoff(round_id=self.round_id)
        self.assertIn("json", handoff)
        self.assertIn("markdown", handoff)
        self.assertEqual(len(handoff["paths"]), 2)
        submitted = mcp_server.submit_changes(round_id=self.round_id, commit=self.commit, request_id="direct-1")
        self.assertEqual(submitted["status"], "accepted")
        history = mcp_server.query_history(task_id=self.task_id)
        self.assertEqual(len(history["claims"]), 1)
        with self.assertRaises(ToolError) as progress_denied:
            mcp_server.progress_summary()
        self.assertEqual(json.loads(str(progress_denied.exception))["message"], "actor_tool_not_allowed")
        with self.assertRaises(ToolError) as caught:
            mcp_server.get_task_context()
        error = json.loads(str(caught.exception))
        self.assertEqual(error["error"], "validation")
        self.assertEqual(error["message"], "task_context_requires_one_selector")

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_next_task_is_not_available_to_secretary(self):
        with patch.dict(os.environ, {"LABOPS_ACTOR": "secretary"}):
            with self.assertRaises(ToolError) as denied:
                mcp_server.next_task()
        self.assertEqual(json.loads(str(denied.exception))["message"], "actor_tool_not_allowed")

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_adopt_process_tool_is_codex_opus_only(self):
        self.assertIn("adopt_process", mcp_server._ACTOR_TOOLS["codex"])
        self.assertIn("adopt_process", mcp_server._ACTOR_TOOLS["opus"])
        self.assertNotIn("adopt_process", mcp_server._ACTOR_TOOLS["secretary"])
        with patch.dict(os.environ, {"LABOPS_ACTOR": "secretary"}):
            with self.assertRaises(ToolError) as denied:
                mcp_server.adopt_process(repo_id="toy", pid=123, title="forbidden")
        self.assertEqual(json.loads(str(denied.exception))["message"], "actor_tool_not_allowed")

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_desktop_tools_actor_permissions_and_worker_parameters(self):
        from inspect import signature
        from labops import mcp_worker

        permissions = {
            "desktop_list": {"codex", "opus", "secretary"},
            "desktop_send": {"opus"},
            "desktop_status": {"opus", "secretary"},
            "desktop_dispatch": {"secretary"},
            "notify_user": {"codex", "opus"},
            "message_claude": {"codex"},
        }
        for name, allowed in permissions.items():
            with self.subTest(tool=name):
                for registry in (mcp_server._ACTOR_TOOLS, mcp_worker._ACTOR_TOOLS):
                    self.assertEqual({actor for actor, names in registry.items() if name in names}, allowed)
                self.assertEqual(tuple(signature(getattr(mcp_server, name)).parameters), mcp_worker.TOOLS[name])
                for actor in {"codex", "opus", "secretary"} - allowed:
                    with self.assertRaises(ToolError) as denied:
                        mcp_server._call_worker(name, {}, actor)
                    self.assertEqual(json.loads(str(denied.exception))["message"], "actor_tool_not_allowed")

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_secretary_desktop_status_excludes_detail(self):
        conn = connect(self.home / "labops.db")
        try:
            conn.execute(
                "INSERT INTO desktop_requests(request_id,kind,target,actor,prompt_sha256,prompt_chars,"
                "state,detail_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("mcp-request", "claude", "fixture", "opus", "sha", 1, "FAILED",
                 json.dumps({"name": "desk", "transcript_path": "fixture-private-path"}), "t", "t"),
            )
        finally:
            conn.close()
        with patch.dict(os.environ, {"LABOPS_ACTOR": "secretary"}):
            status = mcp_server.desktop_status(request_id="mcp-request")
        self.assertEqual((status["request_id"], status["state"], status["name"]), ("mcp-request", "FAILED", "desk"))
        self.assertNotIn("detail", status)
        self.assertNotIn("fixture-private-path", json.dumps(status))

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_codex_notify_user_enqueues_report(self):
        result = mcp_server.notify_user(text="Fixture report", request_id="mcp-report")
        self.assertTrue(result["queued"])
        conn = connect(self.home / "labops.db")
        try:
            row = conn.execute("SELECT kind,text,status FROM notifications WHERE event_key=?",
                               (result["event_key"],)).fetchone()
            self.assertEqual(tuple(row), ("agent_report", "[LabOps · Codex 回报]\nFixture report", "PENDING"))
        finally:
            conn.close()


    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_secretary_can_record_intent_but_cannot_register_entrypoint(self):
        secretary_tools = mcp_server._ACTOR_TOOLS["secretary"]
        self.assertTrue({"queue_list", "queue_intent", "queue_choose"}.issubset(secretary_tools))
        self.assertNotIn("queue_register", secretary_tools)
        with patch.dict(os.environ, {"LABOPS_ACTOR": "secretary"}):
            draft = mcp_server.queue_intent(repo_id="toy", title="intent", note="user request")
            self.assertEqual(draft["queue_item"]["status"], "DRAFT")
            with self.assertRaises(ToolError) as denied:
                mcp_server.queue_register(repo_id="toy", title="forbidden", entrypoint="C:/missing.ps1",
                                          context="should be rejected")
        self.assertEqual(json.loads(str(denied.exception))["message"], "actor_tool_not_allowed")

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_m0c_worktree_snapshot_and_actor_role_tools(self):
        self.assertIsNone(mcp_server.get_worktree(round_id=self.round_id)["dirty_files"])
        task_id = "T20261019-mcp-work"
        conn = connect(self.home / "labops.db")
        try:
            create_task(conn, repo_id="toy", task_id=task_id, goal="M0c worktree", profile={"schema": 1}, base=self.base)
        finally:
            conn.close()
        with self.assertRaises(ToolError) as wrong_codex_role:
            mcp_server.start_work(task_id=task_id, role="fix")
        self.assertEqual(json.loads(str(wrong_codex_role.exception))["message"], "actor_role_not_allowed")
        with self.assertRaises(ToolError) as open_round_error:
            mcp_server.start_work(task_id=self.task_id, role="implement")
        self.assertEqual(json.loads(str(open_round_error.exception))["message"], "round_open_exists")
        with patch.dict(os.environ, {"LABOPS_ACTOR": "opus"}):
            with self.assertRaises(ToolError) as wrong_opus_role:
                mcp_server.start_work(task_id=task_id, role="implement")
            self.assertEqual(json.loads(str(wrong_opus_role.exception))["message"], "actor_role_not_allowed")

        started = mcp_server.start_work(task_id=task_id, role="implement")
        self.assertEqual(started["worktree_status"], "APPLIED")
        self.assertEqual(started["n"], 1)
        empty = mcp_server.get_worktree(round_id=started["round_id"])
        self.assertEqual(empty["dirty_files"], [])
        target = Path(started["worktree_path"]) / "m0c.txt"
        target.write_text("M0c MCP snapshot\n", encoding="utf-8")
        dirty = mcp_server.get_worktree(round_id=started["round_id"])
        self.assertEqual(dirty["dirty_files"], ["m0c.txt"])
        snap = mcp_server.snapshot(round_id=started["round_id"], files=["m0c.txt"], message="M0c snapshot")
        self.assertTrue(snap["commit"])
        self.assertEqual(mcp_server.get_worktree(round_id=started["round_id"])["dirty_files"], [])
        submitted = mcp_server.submit_changes(round_id=started["round_id"], commit=snap["commit"], request_id="m0c-submit")
        self.assertEqual(submitted["status"], "accepted")

        mismatch_task = "T20261019-mcp-snapshot-actor"
        conn = connect(self.home / "labops.db")
        try:
            create_task(conn, repo_id="toy", task_id=mismatch_task, goal="actor mismatch", profile={"schema": 1}, base=self.base)
            mismatch_round = open_round(conn, task_id=mismatch_task, actor="opus", role="fix")
            created = create_worktree(conn, mismatch_round["round_id"])
        finally:
            conn.close()
        (Path(created["path"]) / "mismatch.txt").write_text("wrong actor\n", encoding="utf-8")
        with self.assertRaises(ToolError) as wrong_snapshot_actor:
            mcp_server.snapshot(round_id=mismatch_round["round_id"], files=["mismatch.txt"], message="wrong actor")
        self.assertEqual(json.loads(str(wrong_snapshot_actor.exception))["message"], "actor_mismatch")

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_submit_review_idempotency_and_propose_integration(self):
        submitted = mcp_server.submit_changes(round_id=self.round_id, commit=self.commit, request_id="before-review")
        self.assertEqual(submitted["status"], "accepted")
        conn = connect(self.home / "labops.db")
        try:
            conn.execute("UPDATE repos SET trusted_for_exec=1 WHERE repo_id='toy'")
        finally:
            conn.close()
        conn = connect(self.home / "labops.db")
        try:
            verified = verify_round(conn, self.round_id)
            self.assertEqual(verified["state"], "VERIFIED")
        finally:
            conn.close()

        with patch.dict(os.environ, {"LABOPS_ACTOR": "opus"}):
            first = mcp_server.submit_review(round_id=self.round_id, verdict="PASS", findings=[], request_id="opus-review")
            replay = mcp_server.submit_review(round_id=self.round_id, verdict="PASS", findings=[], request_id="opus-review")
            self.assertEqual(first["review_id"], replay["review_id"])
            self.assertEqual(first["claim_id"], replay["claim_id"])
            self.assertTrue(replay["replayed"])
            with self.assertRaises(ToolError) as payload_conflict:
                mcp_server.submit_review(round_id=self.round_id, verdict="PASS_WITH_NOTES", findings=[], request_id="opus-review")
            self.assertEqual(json.loads(str(payload_conflict.exception))["error"], "idempotency_conflict")

            conn = connect(self.home / "labops.db")
            try:
                gate = gate_check(conn, self.task_id)
                self.assertTrue(gate["pass"], gate)
            finally:
                conn.close()
            integration = mcp_server.propose_integration(task_id=self.task_id)
            self.assertEqual(integration["state"], "AWAITING_HUMAN_APPROVAL")
            self.assertIn("labops integrate approve --integration", integration["approval"])

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_secretary_allowlist_redaction_and_progress(self):
        conn = connect(self.home / "labops.db")
        try:
            set_confidential(conn, "toy", True)
            conn.execute("UPDATE tasks SET goal='SECRET TASK GOAL' WHERE task_id=?", (self.task_id,))
            conn.execute("UPDATE rounds SET worktree_path='C:/private/worktree',branch='secret/branch' WHERE round_id=?",
                         (self.round_id,))
        finally:
            conn.close()
        with patch.dict(os.environ, {"LABOPS_ACTOR": "secretary"}):
            summary = mcp_server.progress_summary()
            self.assertIn(self.task_id, [task["task_id"] for task in summary["tasks"]])
            history = mcp_server.query_history(task_id=self.task_id)
            self.assertEqual(history["task_id"], self.task_id)
            context = mcp_server.get_task_context(task_id=self.task_id)
            encoded = json.dumps(context, ensure_ascii=False)
            self.assertNotIn("SECRET TASK GOAL", encoded)
            self.assertNotIn("C:/private/worktree", encoded)
            self.assertNotIn("secret/branch", encoded)
            with self.assertRaises(ToolError) as denied:
                mcp_server.start_work(task_id=self.task_id, role="implement")
            self.assertEqual(json.loads(str(denied.exception))["message"], "actor_tool_not_allowed")
            with self.assertRaises(ToolError) as denied_submit:
                mcp_server.submit_changes(round_id=self.round_id, commit=self.commit, request_id="secretary-write")
            self.assertEqual(json.loads(str(denied_submit.exception))["message"], "actor_tool_not_allowed")

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_search_history_tool_is_available_to_secretary_and_redacts_confidential_docs(self):
        conn = connect(self.home / "labops.db")
        try:
            rebuild_index(conn, "toy")
            set_confidential(conn, "toy", True)
            rebuild_index(conn, "toy")
        finally:
            conn.close()
        codex_result = mcp_server.search_history(query=self.task_id, repo_id="toy")
        self.assertTrue(any(item["kind"] == "task" and item["ref"] == self.task_id
                            for item in codex_result["results"]))
        with patch.dict(os.environ, {"LABOPS_ACTOR": "secretary"}):
            secretary_result = mcp_server.search_history(query=self.task_id, repo_id="toy")
        task_result = next(item for item in secretary_result["results"] if item["kind"] == "task")
        self.assertEqual(set(task_result), {"repo_id", "kind", "ref", "ts"})

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_worker_utf8_roundtrip_with_gbk_stdio_environment(self):
        conn = connect(self.home / "labops.db")
        try:
            conn.execute("UPDATE tasks SET goal='中文编码查询 should stay intact' WHERE task_id=?", (self.task_id,))
            rebuild_index(conn, "toy")
            conn.execute("UPDATE rounds SET state='VERIFIED',head_commit=?,head_tree=? WHERE round_id=?",
                         (self.commit, "b" * 40, self.round_id))
        finally:
            conn.close()

        with patch.dict(os.environ, {"LABOPS_ACTOR": "opus", "PYTHONIOENCODING": "gbk"}):
            query_result = mcp_server.search_history(query="中文编码", repo_id="toy")
            self.assertTrue(any("中文编码" in item.get("snippet", "") for item in query_result["results"]))
            submitted = mcp_server.submit_review(
                round_id=self.round_id, verdict="REWORK_REQUIRED",
                findings=[{"id": "F-UTF8", "severity": "major", "path": "result.txt",
                           "text": "中文 findings 输入应保持完整"}], request_id="utf8-gbk",
            )
            self.assertEqual(submitted["state"], "REWORK_REQUIRED")
            context = mcp_server.get_task_context(task_id=self.task_id)
            finding = context["sections"]["open_findings"]["findings"][0]
            self.assertEqual(finding["text"], "中文 findings 输入应保持完整")

        conn = connect(self.home / "labops.db")
        try:
            stored = conn.execute("SELECT findings_json FROM reviews WHERE round_id=?", (self.round_id,)).fetchone()[0]
            self.assertIn("中文 findings 输入应保持完整", stored)
        finally:
            conn.close()

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_propose_integration_returns_gate_reasons_without_proposing(self):
        result = mcp_server.propose_integration(task_id=self.task_id)
        self.assertEqual(result["proposed"], False)
        self.assertIn("no_submitted_round", result["reasons"])
        conn = connect(self.home / "labops.db")
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM integrations").fetchone()[0], 0)
        finally:
            conn.close()

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_worker_reload_reads_changed_implementation_without_server_restart(self):
        code_root = self.root / "reload-source"
        shutil.copytree(SRC / "labops", code_root / "labops",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        with patch.dict(os.environ, {"LABOPS_CODE_ROOT": str(code_root), "LABOPS_TEST_ECHO": str(self.root / "echo.txt")}):
            before = mcp_server._call_worker(
                "get_task_context", {"task_id": self.task_id, "round_id": None, "budget_tokens": 4000}, "codex")
            self.assertEqual(before["sections"]["task"]["task_id"], self.task_id)
            worker_path = code_root / "labops" / "mcp_worker.py"
            source = worker_path.read_text(encoding="utf-8")
            old = (
                "return task_context(conn, task_id=args.get(\"task_id\"), round_id=args.get(\"round_id\"),\n"
                "                                budget_tokens=args.get(\"budget_tokens\", 4000))"
            )
            replacement = "return {\"reloaded\": Path(os.environ[\"LABOPS_TEST_ECHO\"]).read_text(encoding=\"utf-8\")}"
            self.assertEqual(source.count(old), 1)
            worker_path.write_text(source.replace(old, replacement), encoding="utf-8")
            echo = Path(os.environ["LABOPS_TEST_ECHO"])
            echo.write_text("worker revision two", encoding="utf-8")
            after = mcp_server._call_worker(
                "get_task_context", {"task_id": self.task_id, "round_id": None, "budget_tokens": 4000}, "codex")
            self.assertEqual(after, {"reloaded": "worker revision two"})

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_worker_timeout_maps_to_worker_failed(self):
        timeout = subprocess.TimeoutExpired([str(PYTHON), "-m", "labops.mcp_worker"], 55, stderr=b"tail line")
        with patch.object(mcp_server.subprocess, "run", side_effect=timeout):
            with self.assertRaises(ToolError) as caught:
                mcp_server._call_worker("get_worktree", {"round_id": self.round_id}, "codex")
        self.assertEqual(json.loads(str(caught.exception)), {"error": "worker_failed"})

    @unittest.skipUnless(MCP_AVAILABLE, "mcp not installed")
    def test_sdk_stdio_roundtrip_and_cli_claim_fields_match(self):
        cli_repo = self.root / "cli-repo"
        shutil.copytree(self.repo, cli_repo)
        cli_home = self.root / "cli-home"
        self._seed_home(cli_home, cli_repo, base=self.base)

        async def roundtrip():
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env.update({"LABOPS_HOME": str(self.home), "LABOPS_ACTOR": "codex"})
            params = StdioServerParameters(command=str(PYTHON), args=["-m", "labops.mcp_server"],
                                           cwd=SRC, env=env)
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    names = {item.name for item in listed.tools}
                    self.assertEqual(names, {"get_task_context", "get_handoff", "get_worktree", "start_work", "snapshot",
                                             "sync_main", "submit_changes", "submit_review", "propose_integration", "query_history",
                                             "progress_summary", "search_history", "next_task", "queue_list", "queue_intent",
                                             "queue_choose", "queue_register", "queue_cancel", "adopt_process",
                                             "desktop_list", "desktop_send", "desktop_status",
                                             "desktop_dispatch", "notify_user", "message_claude"})
                    context_result = await session.call_tool("get_task_context", {"task_id": self.task_id})
                    self.assertFalse(context_result.is_error)
                    submit_result = await session.call_tool("submit_changes", {
                        "round_id": self.round_id,
                        "commit": self.commit,
                        "request_id": "stdio-parity",
                    })
                    self.assertFalse(submit_result.is_error)
                    return submit_result

        result = asyncio.run(roundtrip())
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["LABOPS_HOME"] = str(cli_home)
        cli = subprocess.run(
            [str(PYTHON), "-m", "labops.cli", "submit", "--round", self.round_id, "--actor", "codex",
             "--commit", self.commit, "--request-id", "stdio-parity"],
            cwd=SRC, capture_output=True, env=env, text=True,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr or cli.stdout)
        cli_result = json.loads(cli.stdout)
        self.assertTrue(cli_result["ok"], cli_result)
        mcp_conn = connect(self.home / "labops.db")
        cli_conn = connect(cli_home / "labops.db")
        try:
            fields = ("round_id", "actor", "kind", "client_request_id", "payload_json", "payload_hash",
                      "fingerprint", "status", "duplicate_of", "reject_reason")
            mcp_claim = mcp_conn.execute("SELECT * FROM claims").fetchone()
            cli_claim = cli_conn.execute("SELECT * FROM claims").fetchone()
            self.assertEqual(tuple(mcp_claim[field] for field in fields), tuple(cli_claim[field] for field in fields))
            self.assertEqual(mcp_claim["status"], "accepted")
            self.assertEqual((mcp_claim["source"], cli_claim["source"]), ("mcp", "cli"))
        finally:
            mcp_conn.close()
            cli_conn.close()
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
