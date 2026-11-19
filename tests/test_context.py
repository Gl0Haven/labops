import tempfile
import unittest
from pathlib import Path

from labops.canon import canonical_json
from labops.context import estimate_tokens, task_context
from labops.db import connect, migrate
from labops.tasks import add_repo, create_task, open_round, set_handoff_rules, set_policy, set_task_brief
from labops.redact import for_actor
from tests.helpers import make_repo


class TaskContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-context-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        make_repo(self.repo)
        self.conn = connect(self.root / "home" / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo)
        set_policy(self.conn, "toy", {"schema": 1, "frozen": [{"path": "frozen.py", "symbols": ["keep"], "ref": "policy-ref", "note": "omit"}]})
        self.task_id = "T20261019-context"
        create_task(self.conn, repo_id="toy", task_id=self.task_id, goal="界" * 2400,
                    profile={"schema": 1, "commands": [{"name": "unit", "argv": ["python", "-m", "unittest"],
                                                          "timeout_s": 30}]})
        self.round = open_round(self.conn, task_id=self.task_id, actor="codex", role="implement")

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_cjk_estimate_and_deterministic_section_order(self):
        self.assertEqual(estimate_tokens("中文a"), 3)
        context = task_context(self.conn, round_id=self.round["round_id"])
        self.assertEqual(list(context["sections"]), ["task", "handoff", "current", "open_findings", "last_verification", "policy", "recent_events"])
        self.assertEqual(context["sections"]["current"]["next_action"], "implement_then_snapshot_and_submit")
        self.assertIn("brief", context["sections"]["handoff"])
        self.assertEqual(context["sections"]["policy"]["frozen"], [{"path": "frozen.py", "symbols": ["keep"], "ref": "policy-ref"}])
        self.assertEqual(canonical_json(context), canonical_json(task_context(self.conn, task_id=self.task_id)))
        self.assertLessEqual(context["used_tokens"], context["budget_tokens"])

    def test_exact_budget_truncates_one_section_and_still_tries_later_sections(self):
        complete = task_context(self.conn, task_id=self.task_id, budget_tokens=32000)
        sections = complete["sections"]
        task_cost = estimate_tokens(canonical_json(sections["task"]).decode("utf-8"))
        refs = {
            "task": self.task_id,
            "handoff": self.task_id,
            "current": self.round["round_id"],
            "open_findings": self.task_id,
            "last_verification": self.task_id,
            "policy": self.conn.execute("SELECT policy_hash FROM tasks WHERE task_id=?", (self.task_id,)).fetchone()[0],
            "recent_events": f"{self.task_id}:{complete['snapshot_version']}",
        }
        reserved = sum(estimate_tokens(canonical_json({"ref": refs[name]}).decode("utf-8"))
                       for name in list(sections)[1:])
        exact = task_cost + reserved
        exact_context = task_context(self.conn, task_id=self.task_id, budget_tokens=exact)
        self.assertEqual(exact_context["sections"]["task"], sections["task"])
        self.assertIn("current", exact_context["truncated"])
        self.assertLessEqual(exact_context["used_tokens"], exact)
        below = task_context(self.conn, task_id=self.task_id, budget_tokens=exact - 1)
        self.assertEqual(below["sections"]["task"], {"ref": self.task_id})
        self.assertIsInstance(below["sections"]["current"], dict)
        self.assertNotIn("ref", below["sections"]["current"])
        self.assertLessEqual(below["used_tokens"], exact - 1)

    def test_invalid_selector_and_budget_are_rejected(self):
        with self.assertRaisesRegex(Exception, "task_context_requires_one_selector"):
            task_context(self.conn)
        with self.assertRaisesRegex(Exception, "invalid_context_budget"):
            task_context(self.conn, task_id=self.task_id, budget_tokens=499)

    def test_terminal_task_states_override_round_state(self):
        for task_state, expected in (("INTEGRATED", "done"), ("ABANDONED", "abandoned")):
            with self.subTest(task_state=task_state):
                self.conn.execute("UPDATE tasks SET state=? WHERE task_id=?", (task_state, self.task_id))
                context = task_context(self.conn, task_id=self.task_id)
                self.assertEqual(context["sections"]["current"]["next_action"], expected)

    def test_verified_review_ready_task_waits_for_integration_approval(self):
        self.conn.execute("UPDATE tasks SET state='READY_FOR_INTEGRATION' WHERE task_id=?", (self.task_id,))
        self.conn.execute("UPDATE rounds SET state='VERIFIED',head_commit=?,head_tree=? WHERE round_id=?",
                          ("a" * 40, "b" * 40, self.round["round_id"]))
        profile_hash = self.conn.execute("SELECT profile_hash FROM tasks WHERE task_id=?", (self.task_id,)).fetchone()[0]
        self.conn.execute(
            "INSERT INTO reviews(review_id,task_id,round_id,reviewer,state,verdict,independence,base_commit,reviewed_commit,reviewed_tree,scope_id,acceptance_profile_hash,findings_json,created_at,updated_at) "
            "VALUES('rv-context',?,?,'opus','PASS','PASS','INDEPENDENT','base',?,'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','scope',?,'[]','t','t')",
            (self.task_id, self.round["round_id"], "a" * 40, profile_hash),
        )
        context = task_context(self.conn, task_id=self.task_id)
        self.assertEqual(context["sections"]["current"]["next_action"], "await_integration_approval")

    def test_handoff_brief_rules_and_budget_reference(self):
        brief = "交接内容：先复查中文边界。"
        self.assertTrue(set_task_brief(self.conn, self.task_id, brief)["changed"])
        rules = "- 通用规矩\n- 保留中文"
        self.assertTrue(set_handoff_rules(self.conn, rules)["changed"])
        context = task_context(self.conn, task_id=self.task_id, budget_tokens=32000)
        self.assertEqual(context["sections"]["handoff"], {"rules": rules, "brief": brief})
        self.assertLessEqual(context["used_tokens"], context["budget_tokens"])

        set_task_brief(self.conn, self.task_id, "x" * 8000)
        low_budget = task_context(self.conn, task_id=self.task_id, budget_tokens=500)
        self.assertEqual(low_budget["sections"]["handoff"], {"ref": self.task_id})
        self.assertIn("handoff", low_budget["truncated"])

        with self.assertRaisesRegex(Exception, "task_brief_too_long"):
            set_task_brief(self.conn, self.task_id, "x" * 8001)
        self.conn.execute("UPDATE tasks SET state='INTEGRATED' WHERE task_id=?", (self.task_id,))
        with self.assertRaisesRegex(Exception, "task_brief_not_editable"):
            set_task_brief(self.conn, self.task_id, "closed")

    def test_alert_pause_is_top_level_and_survives_secretary_redaction(self):
        self.conn.execute("UPDATE repos SET confidential=1 WHERE repo_id='toy'")
        self.conn.execute("INSERT INTO meta(key,value) VALUES('wechat_paused_at','2026-10-20T12:00:00Z')")
        context = task_context(self.conn, task_id=self.task_id)
        secretary_context = for_actor(self.conn, "secretary", context)
        expected = {"since": "2026-10-20T12:00:00Z", "hint": "微信凭证失效：请给 bot 发一句话"}
        self.assertEqual(context["alerts_paused"], expected)
        self.assertEqual(secretary_context["alerts_paused"], expected)


if __name__ == "__main__":
    unittest.main()
