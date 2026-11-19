import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from labops import cli
from labops.db import connect, migrate
from labops.errors import ValidationError
from labops.runs import preview_progress_rules, scan_runs
from labops.tasks import set_repo_options


class ProgressRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-progress-rules-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "background-state"
        self.state.mkdir()
        self.conn = connect(self.root / "home" / "labops.db")
        migrate(self.conn)
        self.conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy',?,?)",
                          (str(self.repo), "2026-10-21T10:00:00Z"))

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    @staticmethod
    def _timestamp(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

    def _set_rules(self, *rules):
        return set_repo_options(self.conn, "toy", progress_rule=[
            json.dumps(rule, ensure_ascii=False) for rule in rules
        ])

    def _write_json(self, relative, value, modified):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        stamp = self._timestamp(modified)
        os.utime(path, (stamp, stamp))
        return path

    def _write_run(self, key="a" * 64, *, status="running", output="", modified="2026-10-21T10:59:00Z"):
        state_dir = self.state / key
        state_dir.mkdir(parents=True, exist_ok=True)
        run_dir = self.root / "runs" / f"run-{key[:6]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        log = run_dir / "output.log"
        if output is not None:
            log.write_text(output, encoding="utf-8")
            stamp = self._timestamp(modified)
            os.utime(log, (stamp, stamp))
        (state_dir / "launch.json").write_text(json.dumps({
            "schemaVersion": 4, "runDirectory": str(run_dir), "scriptPath": str(self.repo / "train.py"),
            "workingDirectory": str(self.repo), "requestedAtUtc": "2026-10-21T10:00:00Z",
        }), encoding="utf-8")
        (state_dir / "process.json").write_text(json.dumps({
            "pid": 1234, "processStartedAtUtc": "2026-10-21T10:00:00Z",
            "runDirectory": str(run_dir), "logPath": str(log),
        }), encoding="utf-8")
        (state_dir / "status.json").write_text(json.dumps({
            "status": status, "pid": 1234, "startedAtUtc": "2026-10-21T10:00:00Z",
            "finishedAtUtc": "2026-10-21T11:00:00Z" if status != "running" else None,
            "exitCode": 0 if status == "completed" else None,
        }), encoding="utf-8")
        return state_dir, log

    def _scan(self, when="2026-10-21T11:00:00Z", *, process_info=None):
        return scan_runs(self.conn, state_root=self.state, now_ts=when,
                         process_info=process_info or (lambda _pid: None))

    def test_json_rule_uses_newest_file_path_groups_missing_fields_and_safe_template(self):
        self._write_json("metrics/order_1/arm_A/trajectories/n5/progress.json", {
            "phase": "train", "rows_complete": 20, "total_rows": 100, "fps": 0.16666,
        }, "2026-10-21T10:58:00Z")
        self._write_json("metrics/order_2/arm_VRAND/trajectories/n30/progress.json", {
            "phase": "eval", "rows_complete": 100, "total_rows": 100,
            "fps": 0.16666,
        }, "2026-10-21T10:59:30Z")
        self._write_run(output="step 8/10\n", modified="2026-10-21T10:59:00Z")
        self._set_rules({
            "name": "json", "glob": "metrics/**/progress.json", "type": "json",
            "path_regex": r"(?P<order>order_\d+)/(?P<arm>arm_\w+)/trajectories/(?P<n>n\d+)",
            "template": "ES {order}/{arm} {n} · {phase} {rows_complete}/{total_rows} · {missing} fps={fps} {a.b} {__class__}",
        })

        result = self._scan()
        row = self.conn.execute("SELECT progress_hint FROM bg_runs WHERE run_key=?", ("a" * 64,)).fetchone()
        expected = "ES order_2/arm_VRAND n30 · eval 100/100 · ? fps=0.167 {a.b} ?"
        self.assertEqual(row["progress_hint"], expected)
        self.assertEqual(result["progress_rule_errors"], 0)
        preview = preview_progress_rules(self.conn, "toy")
        self.assertEqual(preview["rules"][0]["file"],
                         "metrics/order_2/arm_VRAND/trajectories/n30/progress.json")
        self.assertEqual(preview["rules"][0]["progress_hint"], expected)
        self.assertEqual(preview["rules"][0]["fields"]["fps"], 0.16666)

    def test_line_regex_reads_last_carriage_return_progress_line(self):
        path = self.repo / "logs" / "trainer.log"
        path.parent.mkdir()
        path.write_bytes(b"warming\rstep 2/12\rstep 10/12\r")
        stamp = self._timestamp("2026-10-21T10:59:45Z")
        os.utime(path, (stamp, stamp))
        self._write_run(output="step 1/12\n", modified="2026-10-21T10:59:00Z")
        self._set_rules({
            "name": "line", "glob": "logs/*.log", "type": "line_regex",
            "pattern": r"step (?P<step>\d+)/(?P<total>\d+)",
            "template": "step {step}/{total}",
        })

        self._scan()
        row = self.conn.execute("SELECT progress_hint FROM bg_runs WHERE run_key=?", ("a" * 64,)).fetchone()
        self.assertEqual(row["progress_hint"], "step 10/12")
        self.assertEqual(preview_progress_rules(self.conn, "toy")["rules"][0]["fields"],
                         {"step": "10", "total": "12"})

    def test_newest_candidate_wins_between_rule_file_and_run_log(self):
        progress = self._write_json("progress/state.json", {"batch": 5}, "2026-10-21T10:59:30Z")
        _state, log = self._write_run(output="step 1/10\n", modified="2026-10-21T10:59:00Z")
        self._set_rules({"name": "json", "glob": "progress/*.json", "type": "json",
                         "template": "batch {batch}"})

        self._scan()
        self.assertEqual(self.conn.execute("SELECT progress_hint FROM bg_runs").fetchone()[0], "batch 5")
        log.write_text("step 7/10\n", encoding="utf-8")
        stamp = self._timestamp("2026-10-21T11:00:05Z")
        os.utime(log, (stamp, stamp))
        self._scan("2026-10-21T11:00:10Z")
        self.assertEqual(self.conn.execute("SELECT progress_hint FROM bg_runs").fetchone()[0], "step 7/10")
        self.assertLess(progress.stat().st_mtime, log.stat().st_mtime)

    def test_finished_run_keeps_last_running_progress(self):
        rule_path = self._write_json("progress/state.json", {"batch": 5}, "2026-10-21T10:59:30Z")
        state, _log = self._write_run(output="step 1/10\n", modified="2026-10-21T10:59:00Z")
        self._set_rules({"name": "json", "glob": "progress/*.json", "type": "json",
                         "template": "batch {batch}"})
        self._scan()
        self.assertEqual(self.conn.execute("SELECT progress_hint FROM bg_runs").fetchone()[0], "batch 5")

        rule_path.write_text(json.dumps({"batch": 20}), encoding="utf-8")
        stamp = self._timestamp("2026-10-21T11:00:05Z")
        os.utime(rule_path, (stamp, stamp))
        status_path = state / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status.update({"status": "completed", "finishedAtUtc": "2026-10-21T11:00:06Z", "exitCode": 0})
        status_path.write_text(json.dumps(status), encoding="utf-8")
        self._scan("2026-10-21T11:00:10Z")
        row = self.conn.execute("SELECT status,progress_hint FROM bg_runs").fetchone()
        self.assertEqual(tuple(row), ("completed", "batch 5"))

        rule_path.write_text(json.dumps({"batch": 30}), encoding="utf-8")
        stamp = self._timestamp("2026-10-21T11:01:05Z")
        os.utime(rule_path, (stamp, stamp))
        self._scan("2026-10-21T11:01:10Z")
        self.assertEqual(self.conn.execute("SELECT progress_hint FROM bg_runs").fetchone()[0], "batch 5")

    def test_bad_json_is_skipped_and_counted_and_validation_rejects_invalid_rules(self):
        bad = self.repo / "progress" / "state.json"
        bad.parent.mkdir()
        bad.write_text("{broken", encoding="utf-8")
        stamp = self._timestamp("2026-10-21T10:59:30Z")
        os.utime(bad, (stamp, stamp))
        self._write_run(output="step 1/10\n", modified="2026-10-21T10:59:00Z")
        self._set_rules({"name": "json", "glob": "progress/*.json", "type": "json",
                         "template": "batch {batch}"})

        result = self._scan()
        self.assertEqual(result["progress_rule_errors"], 1)
        self.assertEqual(self.conn.execute("SELECT progress_hint FROM bg_runs").fetchone()[0], "step 1/10")
        preview = preview_progress_rules(self.conn, "toy")["rules"][0]
        self.assertTrue(preview["error"])

        invalid = [
            {"name": "bad", "glob": "../outside.json", "type": "json", "template": "x"},
            {"name": "bad", "glob": "x.json", "type": "json", "path_regex": "(", "template": "x"},
            {"name": "bad", "glob": "x.log", "type": "line_regex", "pattern": "(", "template": "x"},
            {"name": "bad", "glob": "x.json", "type": "bad", "template": "x"},
            {"name": "bad", "glob": "x.json", "type": ["json"], "template": "x"},
            {"name": "bad", "glob": "x.json", "type": "json", "template": "x" * 201},
        ]
        for rule in invalid:
            with self.subTest(rule=rule):
                with self.assertRaises(ValidationError):
                    set_repo_options(self.conn, "toy", progress_rule=[json.dumps(rule)])
        with self.assertRaises(ValidationError):
            set_repo_options(self.conn, "toy", progress_rule=[json.dumps({
                "name": str(i), "glob": "x", "type": "json", "template": "x",
            }) for i in range(9)])
        cleared = set_repo_options(self.conn, "toy", progress_rule=[""])
        self.assertEqual(cleared["run_progress_rules"], [])

    def test_cli_repo_set_accepts_repeated_rule_json_arguments(self):
        rules = [
            {"name": "a", "glob": "a.json", "type": "json", "template": "a {x}"},
            {"name": "b", "glob": "b.log", "type": "line_regex", "pattern": r"(?P<x>\d+)",
             "template": "b {x}"},
        ]
        argv = ["repo", "set", "--repo-id", "toy"]
        for rule in rules:
            argv.extend(["--progress-rule", json.dumps(rule)])
        args = cli.build_parser().parse_args(argv)
        cli_conn = connect(self.root / "set-home" / "labops.db")
        migrate(cli_conn)
        cli_conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy',?,?)",
                         (str(self.repo), "t"))
        with patch("labops.cli._db", return_value=cli_conn):
            result = cli.dispatch(args)
        self.assertEqual(result["run_progress_rules"], rules)

    def test_adopted_runs_use_repository_progress_rules_and_cli_preview_is_read_only(self):
        self._write_json("progress/state.json", {"batch": 8}, "2026-10-21T10:59:30Z")
        self._set_rules({"name": "json", "glob": "progress/*.json", "type": "json",
                         "template": "batch {batch}"})
        run_dir = self.root / "adopted-run"
        run_dir.mkdir()
        self.conn.execute(
            "INSERT INTO bg_runs(run_key,repo_id,run_dir,status,pid,started_at,progress_hint,progress_at,"
            "source,process_started_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("adopt_4242_1792576800", "toy", str(run_dir), "running", 4242,
             "2026-10-21T10:00:00Z", None, None, "adopted", "2026-10-21T10:00:00Z", "2026-10-21T10:00:00Z"),
        )
        result = self._scan(process_info=lambda _pid: {
            "id": 4242, "start_utc": "2026-10-21T10:00:00Z", "command_line": "toy"
        })
        row = self.conn.execute("SELECT progress_hint FROM bg_runs WHERE source='adopted'").fetchone()
        self.assertEqual(row["progress_hint"], "batch 8")
        self.assertEqual(result["progress_rule_errors"], 0)

        args = cli.build_parser().parse_args(["repo", "progress-preview", "--repo-id", "toy"])
        cli_conn = connect(self.root / "cli-home" / "labops.db")
        migrate(cli_conn)
        cli_conn.execute("INSERT INTO repos(repo_id,path,run_progress_rules,created_at) VALUES(?,?,?,?)",
                         ("toy", str(self.repo), json.dumps([{"name": "json", "glob": "progress/*.json",
                          "type": "json", "template": "batch {batch}"}]), "t"))
        with patch("labops.cli._db", return_value=cli_conn):
            preview = cli.dispatch(args)
        self.assertEqual(preview["rules"][0]["progress_hint"], "batch 8")
        self.assertEqual(len(preview["rules"]), 1)


if __name__ == "__main__":
    unittest.main()
