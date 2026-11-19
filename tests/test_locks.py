import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from labops.errors import Blocked
from labops.locks import file_lock, try_lock

ROOT = Path(__file__).resolve().parents[1]


class LockTests(unittest.TestCase):
    def test_two_processes_serialize(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock = root / "locks" / "same.lock"
            log = root / "events.txt"
            script = "\n".join([
                "import sys,time",
                "from pathlib import Path",
                "from labops.locks import file_lock",
                "p=Path(sys.argv[1]); log=Path(sys.argv[2]); who=sys.argv[3]",
                "with file_lock(p):",
                "    with log.open('a',encoding='utf-8') as f:",
                "        f.write('start '+who+'\\n'); f.flush()",
                "        time.sleep(.2)",
                "        f.write('end '+who+'\\n')",
            ])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "src")
            p1 = subprocess.Popen([sys.executable, "-c", script, str(lock), str(log), "one"], env=env)
            p2 = subprocess.Popen([sys.executable, "-c", script, str(lock), str(log), "two"], env=env)
            self.assertEqual(p1.wait(timeout=10), 0)
            self.assertEqual(p2.wait(timeout=10), 0)
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 4)
            self.assertEqual(lines[0].replace("start", "end"), lines[1])
            self.assertEqual(lines[2].replace("start", "end"), lines[3])
            self.assertNotEqual(lines[0].split()[1], lines[2].split()[1])

    def test_process_exit_releases_lock_and_try_lock_reports_state(self):
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "verify.lock"
            script = "\n".join([
                "import sys,time",
                "from pathlib import Path",
                "from labops.locks import file_lock",
                "with file_lock(Path(sys.argv[1])):",
                "    print('locked',flush=True)",
                "    time.sleep(30)",
            ])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "src")
            proc = subprocess.Popen([sys.executable, "-c", script, str(lock)], stdout=subprocess.PIPE, text=True, env=env)
            try:
                self.assertEqual(proc.stdout.readline().strip(), "locked")
                proc.stdout.close()
                with try_lock(lock) as acquired:
                    self.assertFalse(acquired)
                proc.kill()
                proc.wait(timeout=10)
                with file_lock(lock, timeout_s=1):
                    with try_lock(lock) as acquired:
                        self.assertFalse(acquired)
                with try_lock(lock) as acquired:
                    self.assertTrue(acquired)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)

    def test_nonblocking_failure_is_structured(self):
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "lock"
            with file_lock(lock):
                with self.assertRaises(Blocked):
                    with file_lock(lock, blocking=False):
                        pass


if __name__ == "__main__":
    unittest.main()
