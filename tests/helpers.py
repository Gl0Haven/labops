import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env)
    if check and result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return result


def make_repo(path: Path, *, sha256: bool = False, files: dict[str, str] | None = None) -> str:
    path.mkdir(parents=True, exist_ok=True)
    args = ["-c", "init.defaultBranch=main", "init"]
    if sha256:
        args += ["--object-format=sha256"]
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    git(path, "config", "user.name", "labops-test")
    git(path, "config", "user.email", "t@example.invalid")
    git(path, "config", "core.autocrlf", "false")
    for name, value in (files or {"README.txt": "toy\n"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value.encode("utf-8"))
    git(path, "add", "--all")
    git(path, "commit", "-m", "initial")
    return git(path, "rev-parse", "HEAD").stdout.decode().strip()


def commit_file(repo: Path, name: str, value: str, message: str = "update") -> str:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(value.encode("utf-8"))
    git(repo, "add", "--", name)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.decode().strip()


def run_cli(home: Path, *args: str, cwd: Path | None = None, extra_env: dict[str, str] | None = None):
    env = os.environ.copy()
    env["LABOPS_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT / "src")
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, "-m", "labops.cli", *args], cwd=cwd or ROOT, capture_output=True, env=env)


class TempWorkspace:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="labops-test-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"

    def close(self):
        self.temp.cleanup()

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
