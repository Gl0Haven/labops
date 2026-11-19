import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.helpers import ROOT


class ThreeRoundE2ETests(unittest.TestCase):
    def test_smoke_runs_three_rounds_and_integrates(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke_m0a.py")], cwd=ROOT,
                                capture_output=True, env=env, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        summary = json.loads(result.stdout.decode("utf-8").splitlines()[-1])
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["task_state"], "INTEGRATED")
        self.assertEqual(summary["rounds"], 3)
        self.assertTrue(summary["main_equals_r3_head"])
        self.assertEqual(summary["handoffs"], 3)


if __name__ == "__main__":
    unittest.main()
