import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops import cli
from labops.config import now
from labops.db import connect, migrate
from labops.notify import _event_notification, set_alert_detail
from labops.tasks import add_repo, create_task, open_round, set_confidential
from tests.helpers import commit_file, make_repo


class AlertTextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-alerts-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.base = make_repo(self.repo)
        self.env = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.env.start()
        self.conn = connect(self.home / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo)
        self.task_id = "T20261020-labops-alerts"
        self.task = create_task(self.conn, repo_id="toy", task_id=self.task_id, goal="alert fixtures",
                                profile={"schema": 1})
        self.round = open_round(self.conn, task_id=self.task_id, actor="codex", role="implement")
        self.round_id = self.round["round_id"]
        self.profile_hash = self.task["profile_hash"]

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.temp.cleanup()

    def _event(self, entity_type, entity_id, state, detail=None):
        cursor = self.conn.execute(
            "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (now(), entity_type, entity_id, None, state, "test", json.dumps(detail or {}, ensure_ascii=False)),
        )
        return self.conn.execute("SELECT * FROM events WHERE seq=?", (cursor.lastrowid,)).fetchone()

    def _evidence(self, evidence_id, *, results, status="passed"):
        self.conn.execute(
            "INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,results_json,producer,created_at,finished_at) "
            "VALUES(?,?,'INDEPENDENTLY_EXECUTED',?,?,?,?,?,'test',?,?)",
            (evidence_id, self.round_id, status, self.base, "b" * 40, self.profile_hash,
             json.dumps(results, ensure_ascii=False), now(), now()),
        )

    def _review(self, review_id, *, state, verdict, independence="INDEPENDENT", reviewer="opus", findings=None):
        self.conn.execute(
            "INSERT INTO reviews(review_id,task_id,round_id,reviewer,state,verdict,independence,base_commit,reviewed_commit,reviewed_tree,scope_id,acceptance_profile_hash,findings_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (review_id, self.task_id, self.round_id, reviewer, state, verdict, independence,
             self.base, self.base, "b" * 40, "scope", self.profile_hash,
             json.dumps(findings or [], ensure_ascii=False), now(), now()),
        )

    def _integration(self, integration_id, *, state, candidate_commit=None, candidate_tree=None, main_before=None):
        self.conn.execute(
            "INSERT INTO integrations(integration_id,task_id,candidate_commit,candidate_tree,main_before,state,check_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?, '{}',?,?)",
            (integration_id, self.task_id, candidate_commit or self.base,
             candidate_tree or "c" * 40, main_before or self.base, state, now(), now()),
        )

    def test_submitted_text_snapshot(self):
        head = commit_file(self.repo, "alerted.txt", "changed\n")
        self.conn.execute("UPDATE rounds SET head_commit=? WHERE round_id=?", (head, self.round_id))
        result = _event_notification(self.conn, self._event("round", self.round_id, "CLAIMED"))
        self.assertEqual(result["kind"], "submitted")
        self.assertEqual(result["text"],
                         f"[LabOps · toy] alerts r1 · Codex 已提交\ncommit {head[:8]}，改动 1 个文件\n"
                         f"下一步：LabOps 验证（labops verify --round {self.round_id}）")

    def test_verified_text_snapshot(self):
        self._evidence("ev-alert-pass", results=[
            {"name": "unit", "exit_code": 0, "duration_ms": 1200, "timed_out": False},
            {"name": "lint", "exit_code": 0, "duration_ms": 800, "timed_out": False},
        ])
        result = _event_notification(self.conn, self._event("round", self.round_id, "VERIFIED"))
        self.assertEqual(result["kind"], "verified")
        self.assertEqual(result["text"],
                         "[LabOps · toy] alerts r1 · LabOps 验证通过\n"
                         "unit, lint 全部 exit 0，用时 2.0s\n下一步：转给 Opus 审查\n任务：alert fixtures")

    def test_verify_failed_uses_first_failure_and_log_summary(self):
        log = self.root / "verify.log"
        log.write_text("Ran 7 tests in 0.2s\nFAILED (failures=2)\n", encoding="utf-8")
        self._evidence("ev-alert-fail", status="failed", results=[
            {"name": "unit", "exit_code": 1, "duration_ms": 300, "timed_out": False,
             "log_path": str(log)},
            {"name": "later", "exit_code": None, "skipped": True},
        ])
        result = _event_notification(self.conn, self._event("round", self.round_id, "REJECTED"))
        self.assertEqual(result["kind"], "verify_failed")
        self.assertEqual(result["text"],
                         "[LabOps · toy] alerts r1 · LabOps 验证未通过\n"
                         "unit exit 1；FAILED (failures=2)\n下一步：转给 Codex：调用 next_task 返工\n任务：alert fixtures")

    def test_verify_blocked_text_snapshot(self):
        result = _event_notification(
            self.conn, self._event("round", self.round_id, "BLOCKED", {"reason": "repo_not_trusted_for_exec"}))
        self.assertEqual(result["kind"], "verify_blocked")
        self.assertEqual(result["text"],
                         "[LabOps · toy] alerts r1 · LabOps 验证受阻\n原因 repo_not_trusted_for_exec\n"
                         "下一步：看原因后决定（常见：repo 未信任 / 需 GPU）")

    def test_review_blocking_text_snapshot(self):
        findings = [
            {"id": "HO-1", "severity": "major", "text": "旧 VERIFIED 轮次不应重复派发"},
            {"id": "HO-2", "severity": "blocker", "text": "第二个 blocker"},
            {"id": "N-1", "severity": "note", "text": "不计入阻塞项"},
        ]
        self._review("rv-alert-blocking", state="REWORK_REQUIRED", verdict="REWORK_REQUIRED", findings=findings)
        result = _event_notification(self.conn, self._event("review", "rv-alert-blocking", "REWORK_REQUIRED"))
        self.assertEqual(result["kind"], "review_blocking")
        self.assertEqual(result["text"],
                         "[LabOps · toy] alerts r1 · Opus 审查：REWORK_REQUIRED\n"
                         "HO-1：旧 VERIFIED 轮次不应重复派发；等 2 条\n下一步：转给 Codex：调用 next_task\n任务：alert fixtures")

    def test_review_passed_text_snapshot(self):
        findings = [{"id": "N-1", "severity": "note", "text": "note one"},
                    {"id": "N-2", "severity": "note", "text": "note two"}]
        self._review("rv-alert-pass", state="PASS_WITH_NOTES", verdict="PASS_WITH_NOTES", findings=findings)
        result = _event_notification(self.conn, self._event("review", "rv-alert-pass", "PASS_WITH_NOTES"))
        self.assertEqual(result["kind"], "review_passed")
        self.assertEqual(result["text"],
                         "[LabOps · toy] alerts r1 · Opus 审查通过\n2 条 notes\n下一步：等你批准合并")

    def test_self_review_text_snapshot(self):
        self._review("rv-alert-self", state="PASS", verdict="PASS", independence="SELF_REVIEWED_FIX",
                     reviewer="codex", findings=[{"id": "N-1", "severity": "note", "text": "note"}])
        result = _event_notification(self.conn, self._event("review", "rv-alert-self", "PASS"))
        self.assertEqual(result["text"],
                         f"[LabOps · toy] alerts r1 · 自审通过\n1 条 notes\n"
                         f"下一步：需你接受自审：labops approve self-review --task-id {self.task_id}")

    def test_awaiting_approval_text_snapshot(self):
        self._integration("ig-alert-awaiting", state="AWAITING_HUMAN_APPROVAL",
                          candidate_tree="a" * 40)
        result = _event_notification(
            self.conn, self._event("integration", "ig-alert-awaiting", "AWAITING_HUMAN_APPROVAL"))
        self.assertEqual(result["kind"], "awaiting_approval")
        self.assertEqual(result["text"],
                         f"[LabOps · toy] {self.task_id} r1 · LabOps 等你批准合并\n"
                         f"tree {'a' * 12}\n下一步：labops integrate approve --integration ig-alert-awaiting")

    def test_merged_text_snapshot_and_restart_note(self):
        self._integration("ig-alert-merged", state="MERGED", candidate_commit=self.base,
                          candidate_tree="d" * 40, main_before=self.base)
        event = self._event("integration", "ig-alert-merged", "MERGED")
        with patch("labops.gate.client_restart_requirement", return_value={"client_restart_required": False}):
            result = _event_notification(self.conn, event)
        self.assertEqual(result["kind"], "merged")
        self.assertEqual(result["text"],
                         f"[LabOps · toy] {self.task_id} r1 · LabOps 已合入 main\n{self.base[:8]}")
        with patch("labops.gate.client_restart_requirement", return_value={"client_restart_required": True}):
            restart = _event_notification(self.conn, event)
        self.assertEqual(restart["text"],
                         f"[LabOps · toy] {self.task_id} r1 · LabOps 已合入 main\n{self.base[:8]}\n"
                         "下一步：需重启 Codex/Claude")

    def test_integrity_and_legacy_drift_text_snapshots(self):
        integrity = _event_notification(
            self.conn, self._event("round", self.round_id, "integrity_conflict",
                                   {"ref": "refs/labops/T20261020-labops-alerts/r1/codex/submitted"}))
        self.assertEqual(integrity["kind"], "integrity")
        self.assertEqual(integrity["text"],
                         "[LabOps · toy] alerts r1 · LabOps 对账异常\n"
                         "integrity_conflict：refs/labops/T20261020-labops-alerts/r1/codex/submitted\n"
                         "下一步：找 Opus 看")

        legacy = _event_notification(
            self.conn, self._event("repo", "toy", "legacy_drift",
                                   {"kind": "baseline_diverged", "baseline": "a" * 40, "head": "b" * 40}))
        self.assertEqual(legacy["kind"], "legacy_drift")
        self.assertEqual(legacy["text"],
                         "[LabOps · toy] 旧流程 · 旧流程状态漂移\n"
                         f"baseline_diverged：baseline {'a' * 8} / HEAD {'b' * 8}\n"
                         "下一步：可忽略或更新 agent-status")

    def test_operation_conflict_uses_task_and_operation_summary(self):
        self.conn.execute(
            "INSERT INTO operations(op_id,repo_id,kind,ref,status,idempotency_id,detail_json,created_at,updated_at) "
            "VALUES('op-alert-conflict','toy','ff_main','refs/heads/main','CONFLICT','alert-op',?,?,?)",
            (json.dumps({"task_id": self.task_id, "reason": "main_advanced"}), now(), now()),
        )
        result = _event_notification(self.conn, self._event("operation", "op-alert-conflict", "CONFLICT"))
        self.assertEqual(result["kind"], "integrity")
        self.assertEqual(result["text"],
                         "[LabOps · toy] alerts r1 · LabOps 对账异常\n"
                         "main_advanced：refs/heads/main\n下一步：找 Opus 看")

    def test_alert_detail_off_and_truncation_keep_first_line(self):
        set_confidential(self.conn, "toy", True)
        long_event = self._event("round", self.round_id, "BLOCKED", {"reason": "R" * 240})
        expanded = _event_notification(self.conn, long_event)
        lines = expanded["text"].splitlines()
        self.assertLessEqual(len(expanded["text"]), 140)
        self.assertEqual(lines[0], "[LabOps · toy] alerts r1 · LabOps 验证受阻")
        self.assertTrue(lines[1].endswith("…"))

        set_alert_detail(self.conn, "toy", False)
        short = _event_notification(self.conn, long_event)
        self.assertEqual(short["text"], lines[0])

    def test_repo_set_alert_detail_cli_toggle(self):
        off = cli.dispatch(cli.build_parser().parse_args(
            ["repo", "set", "--repo-id", "toy", "--alert-detail", "off"]))
        self.assertFalse(off["alert_detail"])
        self.assertTrue(off["changed"])
        on = cli.dispatch(cli.build_parser().parse_args(
            ["repo", "set", "--repo-id", "toy", "--alert-detail", "on"]))
        self.assertTrue(on["alert_detail"])
        self.assertTrue(on["changed"])


if __name__ == "__main__":
    unittest.main()
