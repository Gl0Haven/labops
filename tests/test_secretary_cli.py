import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops import cli
from labops.db import connect, migrate
from labops.errors import Blocked
from labops.notify import TEST_MESSAGE
from labops.tasks import add_repo, create_task, open_round
from tests.helpers import make_repo


class SecretaryCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-secretary-cli-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        make_repo(self.repo)
        self.env = patch.dict(os.environ, {"LABOPS_HOME": str(self.home)})
        self.env.start()
        conn = connect(self.home / "labops.db")
        try:
            migrate(conn)
            add_repo(conn, repo_id="toy", path=self.repo)
            create_task(conn, repo_id="toy", task_id="T20261020-cli", goal="cli", profile={"schema": 1})
            open_round(conn, task_id="T20261020-cli", actor="codex", role="implement")
        finally:
            conn.close()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_repo_confidential_command_and_confirmation_for_disabling(self):
        args = cli.build_parser().parse_args(["repo", "set", "--repo-id", "toy", "--confidential", "on"])
        self.assertEqual(cli.dispatch(args), {"repo_id": "toy", "confidential": True, "changed": True})
        conn = connect(self.home / "labops.db")
        try:
            self.assertEqual(conn.execute("SELECT confidential FROM repos WHERE repo_id='toy'").fetchone()[0], 1)
        finally:
            conn.close()
        disable = cli.build_parser().parse_args(["repo", "set", "--repo-id", "toy", "--confidential", "off"])
        with self.assertRaises(Blocked):
            cli.dispatch(disable)

    def test_progress_and_notify_cli_commands_are_available_without_sending(self):
        progress = cli.dispatch(cli.build_parser().parse_args(["progress", "--since-hours", "24"]))
        self.assertEqual(progress["tasks"][0]["task_id"], "T20261020-cli")
        self.assertEqual(progress["tasks"][0]["next_action"], "implement_then_snapshot_and_submit")
        notify_run = cli.dispatch(cli.build_parser().parse_args(["notify", "run"]))
        self.assertEqual(notify_run["scanned"], 0)
        self.assertEqual(notify_run["attempted"], 0)
        notify_list = cli.dispatch(cli.build_parser().parse_args(["notify", "list", "--status", "PENDING"]))
        self.assertEqual(notify_list["notifications"], [])

    def test_notify_test_send_uses_fixed_message_and_propagates_failure(self):
        args = cli.build_parser().parse_args(["notify", "test-send"])
        messages = []

        def sender(text):
            messages.append(text)
            return "sent", None

        self.assertEqual(cli.dispatch(args, notify_sender=sender), {"status": "sent", "text": TEST_MESSAGE})
        self.assertEqual(messages, [TEST_MESSAGE])
        with self.assertRaises(Blocked) as failed:
            cli.dispatch(args, notify_sender=lambda text: ("unknown", "timeout"))
        self.assertEqual(str(failed.exception), "notify_test_send_failed")
        self.assertEqual(failed.exception.detail["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
