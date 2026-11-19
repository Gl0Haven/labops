import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops import cli
from labops.config import now
from labops.db import connect, migrate
from labops.report import stats_report, write_markdown
from labops.tasks import add_repo, create_task, open_round
from tests.helpers import make_repo


class StatsReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-report-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.base = make_repo(self.repo)
        self.conn = connect(self.root / "home" / "labops.db")
        migrate(self.conn)
        add_repo(self.conn, repo_id="toy", path=self.repo)
        self.tasks = {}
        for task_id in ("T20261020-stats-active", "T20261020-stats-integrated", "T20261020-stats-created"):
            self.tasks[task_id] = create_task(self.conn, repo_id="toy", task_id=task_id,
                                               goal="stats fixture", profile={"schema": 1})

        self.round1 = open_round(self.conn, task_id="T20261020-stats-active", actor="codex", role="implement")
        self.conn.execute("UPDATE rounds SET state='REJECTED' WHERE round_id=?", (self.round1["round_id"],))
        self.conn.execute("UPDATE tasks SET created_at='2026-10-20T08:00:00Z',updated_at='2026-10-20T08:00:00Z' WHERE task_id='T20261020-stats-active'")
        self.round2 = open_round(self.conn, task_id="T20261020-stats-active", actor="codex", role="implement")

        self.integrated_round = open_round(self.conn, task_id="T20261020-stats-integrated", actor="codex", role="implement")
        self.conn.execute("UPDATE rounds SET state='VERIFIED',head_commit=?,head_tree=? WHERE round_id=?",
                          (self.base, "b" * 40, self.integrated_round["round_id"]))
        self.conn.execute("UPDATE tasks SET state='INTEGRATED',created_at='2026-10-20T09:00:00Z',updated_at='2026-10-20T11:00:00Z' WHERE task_id='T20261020-stats-integrated'")
        self.conn.execute(
            "INSERT INTO integrations(integration_id,task_id,candidate_commit,candidate_tree,main_before,state,check_json,created_at,updated_at) "
            "VALUES('ig-stats',?,?,?,?, 'MERGED','{}','2026-10-20T10:00:00Z','2026-10-20T11:00:00Z')",
            ("T20261020-stats-integrated", self.base, "b" * 40, self.base),
        )
        self.conn.execute(
            "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) "
            "VALUES('2026-10-20T11:00:00Z','task','T20261020-stats-integrated','ACTIVE','INTEGRATED','labops','{}')"
        )
        self.conn.execute("UPDATE tasks SET created_at='2026-10-20T09:30:00Z' WHERE task_id='T20261020-stats-created'")

        self._review("rv_stats_rework", "T20261020-stats-active", self.round1["round_id"], "REWORK_REQUIRED",
                     "INDEPENDENT", [{"id": "R-1", "severity": "major"}, {"id": "R-2", "severity": "note"}])
        self._review("rv_stats_self", "T20261020-stats-integrated", self.integrated_round["round_id"], "PASS",
                     "SELF_REVIEWED_FIX", [{"id": "R-3", "severity": "blocker"}])
        for claim_id, request_id in (("cl_stats_cli", "cli"), ("cl_stats_mcp", "codex-r1-final")):
            self.conn.execute(
                "INSERT INTO claims(claim_id,round_id,actor,kind,client_request_id,payload_json,payload_hash,fingerprint,status,created_at) "
                "VALUES(?,?, 'codex','submit_changes',?,'{}','hash',?,'accepted','2026-10-20T10:00:00Z')",
                (claim_id, self.round1["round_id"], request_id, claim_id),
            )
        for evidence_id, status, duration in (("ev_stats_pass", "passed", 100), ("ev_stats_fail", "failed", 300)):
            self.conn.execute(
                "INSERT INTO evidence(evidence_id,round_id,kind,status,commit_oid,tree_oid,profile_hash,results_json,env_json,producer,created_at,finished_at) "
                "VALUES(?,?,'INDEPENDENTLY_EXECUTED',?,?,?,?,?,'{}','test','2026-10-20T10:00:00Z','2026-10-20T10:01:00Z')",
                (evidence_id, self.integrated_round["round_id"], status, self.base, "b" * 40,
                 self.tasks["T20261020-stats-integrated"]["profile_hash"], json.dumps([{"duration_ms": duration}])) ,
            )
        self.conn.execute(
            "INSERT INTO notifications(event_key,task_id,kind,text,status,created_at) VALUES('alert-pending',NULL,'x','x','PENDING','2026-10-20T10:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO notifications(event_key,task_id,kind,text,status,created_at) VALUES('alert-unknown',NULL,'x','x','UNKNOWN','2026-10-20T10:00:00Z')"
        )
        self.conn.execute("UPDATE notifications SET ntfy_status='ntfy_failed' WHERE event_key='alert-unknown'")
        self.conn.execute(
            "INSERT INTO backups(backup_id,repo_id,path,head,ref_count,sha256,verified,created_at) "
            "VALUES('bk-stats','toy','unused.bundle',?,1,'hash',1,'2026-10-20T10:00:00Z')", (self.base,)
        )

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _review(self, review_id, task_id, round_id, verdict, independence, findings):
        self.conn.execute(
            "INSERT INTO reviews(review_id,task_id,round_id,reviewer,state,verdict,independence,base_commit,reviewed_commit,reviewed_tree,scope_id,acceptance_profile_hash,findings_json,created_at,updated_at) "
            "VALUES(?,?,?,'opus',?,?,?,? ,? ,?,'scope',? ,?,'2026-10-20T10:00:00Z','2026-10-20T10:01:00Z')",
            (review_id, task_id, round_id, verdict, verdict, independence, self.base, self.base,
             "b" * 40, self.tasks[task_id]["profile_hash"], json.dumps(findings)),
        )

    def test_stats_report_aggregates_three_task_fixture(self):
        report = stats_report(self.conn)
        self.assertEqual(report["tasks"]["total"], 3)
        self.assertEqual(report["tasks"]["by_state"], {"ACTIVE": 1, "CREATED": 1, "INTEGRATED": 1})
        self.assertEqual(report["tasks"]["rounds_total"], 3)
        self.assertEqual(report["tasks"]["rework_rounds_total"], 1)
        self.assertEqual(report["submissions"]["claim_count"], 2)
        self.assertIsNone(report["submissions"]["mcp_claim_share"])
        self.assertEqual(report["reviews"]["by_verdict"], {"PASS": 1, "REWORK_REQUIRED": 1})
        self.assertEqual(report["reviews"]["findings_by_severity"], {
            "blocker": 1, "major": 1, "minor": 0, "note": 1,
        })
        self.assertEqual(report["reviews"]["self_review_count"], 1)
        self.assertEqual(report["verification"]["count"], 2)
        self.assertEqual(report["verification"]["pass_rate"], 0.5)
        self.assertEqual(report["verification"]["median_duration_ms"], 200)
        self.assertEqual(report["verification"]["p90_duration_ms"], 300)
        self.assertEqual(report["integrations"]["merged_count"], 1)
        self.assertEqual(report["integrations"]["median_time_to_integrated_seconds"], 7200)
        self.assertEqual(report["alerts"]["by_status"], {"PENDING": 1, "UNKNOWN": 1})
        self.assertEqual(report["alerts"]["ntfy_by_status"], {"ntfy_failed": 1})
        self.assertEqual(report["repositories"]["registered_count"], 1)
        self.assertEqual(report["repositories"]["backup_count"], 1)
        self.assertIsNone(report["repositories"]["last_verify_restore"])

    def test_since_and_markdown_output(self):
        report = stats_report(self.conn, since="2026-10-21")
        self.assertEqual(report["tasks"]["total"], 0)
        self.assertEqual(report["verification"]["count"], 0)
        output = self.root / "reports" / "stats.md"
        self.assertEqual(write_markdown(report, output), str(output.resolve()))
        markdown = output.read_text(encoding="utf-8")
        self.assertIn("# LabOps stats", markdown)
        self.assertIn("Last verify-restore: unknown", markdown)
        with self.assertRaisesRegex(Exception, "invalid_stats_since"):
            stats_report(self.conn, since="2026/10/21")
        with self.assertRaisesRegex(Exception, "invalid_stats_since"):
            stats_report(self.conn, since="2026-W43-1")

    def test_stats_report_counts_rewrite_outcomes_and_background_runs(self):
        self.conn.execute(
            "INSERT INTO notifications(event_key,kind,text,status,created_at,rewritten_text,rewrite_outcome) "
            "VALUES('ev:rewrite-stats','run_failed','template','PENDING','2026-10-20T12:00:00Z','template','template_error')"
        )
        self.conn.execute(
            "INSERT INTO bg_runs(run_key,repo_id,run_dir,status,updated_at) VALUES(?,?,?,?,?)",
            ("a" * 64, "toy", str(self.repo / "runs" / "trial"), "running", "2026-10-20T12:00:00Z"),
        )
        report = stats_report(self.conn)
        self.assertEqual(report["alerts"]["rewrite_by_outcome"], {"template_error": 1})
        self.assertEqual(report["runs"], {"tracked_count": 1, "by_status": {"running": 1}})

    def test_cli_registers_report_stats_options(self):
        args = cli.build_parser().parse_args(["report", "stats", "--since", "2026-10-01", "--out", "stats.md"])
        self.assertEqual(args.command, "report")
        self.assertEqual(args.report_command, "stats")
        self.assertEqual(args.since, "2026-10-01")
        self.assertEqual(args.out, "stats.md")

    def test_cli_persists_last_verify_restore_result_for_report(self):
        db_path = self.root / "home" / "labops.db"
        args = cli.build_parser().parse_args(["backup", "verify-restore", "--repo-id", "toy", "--backup-id", "bk-stats"])
        with patch("labops.cli._db", return_value=self.conn), \
             patch("labops.cli.verify_restore", return_value={"backup_id": "bk-stats", "ok": True,
                                                               "missing": [], "mismatched": []}):
            result = cli.dispatch(args)
        self.assertTrue(result["ok"])
        self.conn = connect(db_path)
        migrate(self.conn)
        report = stats_report(self.conn)
        self.assertEqual(report["repositories"]["last_verify_restore"]["repo_id"], "toy")
        self.assertEqual(report["repositories"]["last_verify_restore"]["backup_id"], "bk-stats")
        self.assertTrue(report["repositories"]["last_verify_restore"]["ok"])


if __name__ == "__main__":
    unittest.main()
