import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labops.canon import canonical_json, sha256_hex
from labops.config import home, is_test_instance, now
from labops.errors import ValidationError
from labops.ids import new_id, round_id, validate_task_id
from tests.helpers import run_cli


class CanonIdTests(unittest.TestCase):
    def test_canonical_json_is_stable_and_utf8(self):
        self.assertEqual(canonical_json({"z": 1, "中文": [True, None]}), '{"z":1,"中文":[true,null]}'.encode())
        self.assertEqual(canonical_json({"b": 2, "a": 1}), canonical_json({"a": 1, "b": 2}))
        self.assertEqual(len(sha256_hex(b"payload")), 64)

    def test_id_and_round_formats(self):
        value = new_id("op")
        self.assertRegex(value, r"^op_\d{8}T\d{6}_[0-9a-f]{8}$")
        self.assertEqual(round_id("T20261019-labops-m0a", 3), "T20261019-labops-m0a/r3")
        with self.assertRaises(ValidationError):
            validate_task_id("task")

    def test_home_rejects_unc_and_fake_time_only_in_test_instance(self):
        with patch.dict(os.environ, {"LABOPS_HOME": r"\\server\share\labops"}):
            with self.assertRaisesRegex(ValidationError, "unc_not_allowed"):
                home()
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"LABOPS_HOME": td, "LABOPS_FAKE_NOW": "2001-02-03T04:05:06Z"}):
                self.assertFalse(is_test_instance())
                self.assertRegex(now(), r"^\d{4}-\d\d-\d\dT")
                init_result = run_cli(Path(td), "init", "--test-instance")
                self.assertEqual(init_result.returncode, 0, init_result.stdout.decode("utf-8", "replace"))
                self.assertEqual(now(), "2001-02-03T04:05:06Z")


if __name__ == "__main__":
    unittest.main()
