import json
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from labops import cli, gpuq, notify
from labops.db import migrate
from labops.errors import ValidationError
from tests.helpers import ROOT


class PublicTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="labops-public-")))
        self.conn = self.enterContext(closing(sqlite3.connect(":memory:", isolation_level=None)))
        self.conn.row_factory = sqlite3.Row
        migrate(self.conn)
        self.enterContext(patch.dict(notify._SENDERS))
        self.enterContext(patch.dict(os.environ, {
            "LABOPS_HOME": str(self.root / "home"),
            "LABOPS_NOTIFY_CHANNEL": "",
            "LABOPS_NOTIFY_SENDER": "",
        }))
        self.repo = self.root / "toy"
        self.repo.mkdir()
        self.entry = self.repo / "train.ps1"
        self.entry.write_text("exit 0\n", encoding="utf-8")
        self.conn.execute("INSERT INTO repos(repo_id,path,created_at) VALUES('toy',?,'t')", (str(self.repo),))

    def test_entry_roots_order_deduplicate_and_clear(self):
        roots = [str(self.repo), str(self.root)]
        self.assertEqual(gpuq.queue_config(self.conn, entry_roots=roots + roots)["entry_roots"], roots)
        self.assertEqual(gpuq.queue_config(self.conn)["entry_roots"], roots)
        self.assertEqual(gpuq.queue_config(self.conn, clear_entry_roots=True)["entry_roots"], [])

    def test_invalid_entry_roots(self):
        for value in ("relative", str(self.root / "missing"), r"\\server\x"):
            with self.subTest(path=value), self.assertRaisesRegex(ValidationError, "invalid_entry_root") as error:
                gpuq.queue_config(self.conn, entry_roots=[value])
            self.assertEqual(error.exception.detail, {"path": value})

    def test_launcher_can_be_set_read_and_cleared(self):
        self.assertEqual(gpuq.queue_config(self.conn, launcher=str(self.entry))["launcher"], str(self.entry))
        self.assertEqual(gpuq.queue_config(self.conn)["launcher"], str(self.entry))
        self.assertIsNone(gpuq.queue_config(self.conn, clear_launcher=True)["launcher"])

    def test_invalid_launcher(self):
        text = self.repo / "file.txt"
        text.touch()
        for path in (text, self.repo / "missing.ps1", Path("relative.ps1")):
            with self.subTest(path=path), self.assertRaisesRegex(ValidationError, "invalid_launcher"):
                gpuq.queue_config(self.conn, launcher=str(path))

    def test_entrypoint_under_repo_without_config_is_allowed(self):
        resolved, digest = gpuq._validate_entrypoint(self.conn, "toy", str(self.entry))
        self.assertEqual(Path(resolved), self.entry.resolve())
        self.assertEqual(len(digest), 64)

    def test_entry_roots_restrict_entrypoint(self):
        other = self.root / "other"
        other.mkdir()
        with patch.object(gpuq, "_ALLOWED_ENTRY_ROOTS", (self.repo,)):
            gpuq.queue_config(self.conn, entry_roots=[str(other)])
            with self.assertRaisesRegex(ValidationError, "queue_entrypoint_outside_allowed_roots"):
                gpuq._validate_entrypoint(self.conn, "toy", str(self.entry))

    def test_entrypoint_outside_repo_is_rejected(self):
        other = self.root / "outside.ps1"
        other.touch()
        with self.assertRaisesRegex(ValidationError, "queue_entrypoint_outside_repo"):
            gpuq._validate_entrypoint(self.conn, "toy", str(other))

    def test_no_launcher_does_not_spawn(self):
        with patch("labops.gpuq.subprocess.run") as run:
            self.assertEqual(gpuq._invoke_launcher({}, "fixture-run", None),
                             {"ok": False, "error": "launcher_not_configured"})
        run.assert_not_called()

    def test_manual_command_has_launcher_placeholder(self):
        queue = {"entrypoint": str(self.entry), "working_dir": str(self.repo)}
        self.assertIn("<launcher.ps1>", gpuq._manual_command(queue, "fixture-run", None))

    def test_openclaw_default_channel(self):
        sender = notify.make_sender()
        self.assertIsInstance(sender, notify.OpenClawSender)
        self.assertEqual((sender.channel, sender.markdown_style), ("openclaw-weixin", "wechat"))

    def test_openclaw_channel_reaches_argv(self):
        with patch.dict(os.environ, {"LABOPS_NOTIFY_CHANNEL": "telegram", "LABOPS_NOTIFY_ACCOUNT": "fixture",
                                     "LABOPS_NOTIFY_TARGET": "fixture-target"}), patch(
                "labops.notify._openclaw_command", return_value=["fake-openclaw"]), patch(
                "labops.notify.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, '{"deliveryStatus":"sent"}', "")) as run:
            sender = notify.OpenClawSender()
            self.assertEqual((sender.channel, sender.markdown_style), ("telegram", None))
            self.assertEqual(sender("fixture"), ("sent", None))
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--channel") + 1], "telegram")

    def test_sender_meta_overrides_env(self):
        env_sender, meta_sender = object(), object()
        notify.register_sender("env-sender", lambda: env_sender)
        notify.register_sender("meta-sender", lambda: meta_sender)
        with patch.dict(os.environ, {"LABOPS_NOTIFY_SENDER": "env-sender"}):
            self.assertIs(notify.make_sender(self.conn), env_sender)
            notify.notify_config(self.conn, sender="meta-sender")
            self.assertIs(notify.make_sender(self.conn), meta_sender)
            self.assertIs(notify.make_sender(), env_sender)

    def test_invalid_and_unknown_sender(self):
        with self.assertRaisesRegex(ValidationError, "invalid_sender_name"):
            notify.register_sender("Invalid Sender", object)
        with patch.dict(os.environ, {"LABOPS_NOTIFY_SENDER": "missing"}):
            with self.assertRaisesRegex(ValidationError, "unknown_notify_sender") as error:
                notify.make_sender()
        self.assertIn("openclaw", error.exception.detail["available"])
        with self.assertRaisesRegex(ValidationError, "unknown_notify_sender"):
            notify.notify_config(self.conn, sender="missing")

    def test_send_pending_checks_markdown_capability(self):
        template = "[LabOps] fixture\none line"
        for index, style in enumerate(("wechat", None)):
            self.conn.execute(
                "INSERT INTO notifications(event_key,kind,text,status,created_at) VALUES(?,? ,?,'PENDING','t')",
                (f"fixture:{index}", "verify_failed", template),
            )
            sent = []

            def sender(text):
                sent.append(text)
                return "sent", None

            if style:
                sender.markdown_style = style
            result = notify.send_pending(self.conn, sender, ntfy_topic="")
            self.assertEqual(result["accepted"], 1)
            self.assertEqual(sent, [notify._to_wechat_markdown(template) if style else template])

    def test_cli_queue_config_dispatch(self):
        parser = cli.build_parser()
        result = cli.dispatch(parser.parse_args(["queue", "config", "--entry-root", str(self.repo),
                                                 "--launcher", str(self.entry)]))
        self.assertEqual(result, {"entry_roots": [str(self.repo)], "launcher": str(self.entry)})
        self.assertEqual(cli.dispatch(parser.parse_args(["queue", "config"])), result)

    def test_cli_notify_config_sender_dispatch(self):
        sender = object()
        notify.register_sender("fixture-sender", lambda: sender)
        cli.dispatch(cli.build_parser().parse_args(["notify", "config", "--sender", "fixture-sender"]))
        with closing(cli._db()) as conn:
            self.assertIs(notify.make_sender(conn), sender)

    def test_windows_ci_workflow_is_parseable(self):
        workflow = json.loads((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        self.assertEqual(workflow["on"], ["push", "pull_request"])
        job = workflow["jobs"]["test"]
        self.assertEqual(job["runs-on"], "windows-latest")
        self.assertEqual(job["strategy"]["matrix"]["python"], ["3.11", "3.13"])
        self.assertTrue(any(step.get("run") == "python -m unittest discover -s tests -t ." for step in job["steps"]))

    def test_public_files_have_no_private_identifiers(self):
        # Generic patterns live here. Maintainer-specific ones (home paths, private project names)
        # go in the gitignored tests/private_patterns.local.txt, one regex per line, so the guard
        # itself does not publish them.
        patterns = [r"sk-[A-Za-z0-9]{20,}", r"gh[pousr]_[A-Za-z0-9]{30,}",
                    r"[\w.+-]+@(?:gmail|qq|163|126|outlook|hotmail)\.com"]
        local = ROOT / "tests" / "private_patterns.local.txt"
        if local.is_file():
            patterns += [line.strip() for line in local.read_text(encoding="utf-8").splitlines()
                         if line.strip() and not line.lstrip().startswith("#")]
        pattern = re.compile("|".join(f"(?:{item})" for item in patterns), re.IGNORECASE)
        files = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
             "src", "tests", "scripts", "examples", "pyproject.toml", "README.md", "README.zh-CN.md", "LICENSE", ".github"],
            cwd=ROOT, capture_output=True, check=True,
        ).stdout.decode("utf-8").split("\0")
        self.assertTrue(all(f"tests/fixtures/legacy_v{version}.sql" in files for version in (1, 2, 3)))
        matches = [name for name in files if name and name != "tests/test_public.py"
                   and pattern.search((ROOT / name).read_text(encoding="utf-8"))]
        self.assertEqual(matches, [])
