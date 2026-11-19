"""Regenerate migration fixtures from the private historical Git objects.

Run this script with the LabOps test Python from the repository root.
It runs toy setup only in a temporary directory. In a clean public snapshot,
the old commits are absent and git archive is expected to fail.
"""

import io
import re
import sys
import os
import sqlite3
import subprocess
import shutil
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / part) for part in ('src', '')]
from labops.canon import canonical_json
from tests.helpers import ROOT, make_repo

_M0A_BASE = "588fd063ef820cfa1c5f0ddc5a0f0cde2eea0a99"
_M0C_V2_BASE = "55a358d96800da87a0a1fbf71ed24f1fdc4ac7bf"
_M1_V3_BASE = "8e9ed94e8b354e576559dfad382429549d831f37"
_VENV_PYTHON = Path(os.environ.get("LABOPS_TEST_PYTHON") or sys.executable)


def _make_m0a_v1_db(root: Path) -> tuple[sqlite3.Connection, str]:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", _M0A_BASE, "src/labops"],
                             capture_output=True, check=True).stdout
    legacy_src = root / "legacy" / "src"
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        for member in source.getmembers():
            parts = Path(member.name).parts
            if not parts or parts[0] != "src":
                continue
            target = legacy_src.joinpath(*parts[1:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.extractfile(member).read())

    repo = root / "repo"
    make_repo(repo, files={"src/frozen.py": "def f():\n    return 1\n"})
    db_path = root / "home" / "labops.db"
    db_path.parent.mkdir(parents=True)
    script = legacy_src / "create_v1.py"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from labops.db import connect, migrate\n"
        "from labops.tasks import add_repo, create_task, set_policy\n"
        "repo, db_path = Path(sys.argv[1]), Path(sys.argv[2])\n"
        "policy = {'schema': 1, 'frozen': [{'path': 'src/frozen.py', 'symbols': ['f'], 'ref': 'review', 'note': 'M0a fixture'}]}\n"
        "conn = connect(db_path)\n"
        "migrate(conn)\n"
        "add_repo(conn, repo_id='toy', path=repo)\n"
        "set_policy(conn, 'toy', policy)\n"
        "create_task(conn, repo_id='toy', task_id='T20261019-migrate', goal='M0a migration fixture', profile={'schema': 1})\n"
        "conn.close()\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run([str(_VENV_PYTHON), str(script), str(repo), str(db_path)], cwd=legacy_src,
                            capture_output=True, text=True, env=env)
    if result.returncode:
        raise AssertionError(f"M0a v1 fixture failed: {result.stderr}\n{result.stdout}")
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    expected_policy = canonical_json({"schema": 1, "frozen": [{"path": "src/frozen.py", "symbols": ["f"],
                                                                      "ref": "review", "note": "M0a fixture"}]}).decode()
    return conn, expected_policy


def _make_v2_db(root: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", _M0C_V2_BASE, "src/labops"],
                             capture_output=True, check=True).stdout
    legacy_src = root / "legacy-v2" / "src"
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        for member in source.getmembers():
            parts = Path(member.name).parts
            if not parts or parts[0] != "src":
                continue
            target = legacy_src.joinpath(*parts[1:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.extractfile(member).read())
    repo = root / "v2-repo"
    make_repo(repo)
    db_path = root / "v2-home" / "labops.db"
    db_path.parent.mkdir(parents=True)
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from labops.db import connect, migrate\n"
        "from labops.tasks import add_repo, create_task, open_round\n"
        "repo, db_path = Path(sys.argv[1]), Path(sys.argv[2])\n"
        "conn = connect(db_path)\n"
        "migrate(conn)\n"
        "add_repo(conn, repo_id='toy-v2', path=repo)\n"
        "create_task(conn, repo_id='toy-v2', task_id='T20261020-v2-copy', goal='v2 fixture', profile={'schema': 1})\n"
        "open_round(conn, task_id='T20261020-v2-copy', actor='codex', role='implement')\n"
        "conn.close()\n"
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run([str(_VENV_PYTHON), "-c", script, str(repo), str(db_path)],
                            cwd=legacy_src, capture_output=True, text=True, env=env)
    if result.returncode:
        raise AssertionError(f"v2 fixture failed: {result.stderr}\n{result.stdout}")
    return db_path


def _make_v3_db_copy(root: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", _M1_V3_BASE, "src/labops"],
                             capture_output=True, check=True).stdout
    legacy_src = root / "live-v3" / "src"
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        for member in source.getmembers():
            parts = Path(member.name).parts
            if not parts or parts[0] != "src":
                continue
            target = legacy_src.joinpath(*parts[1:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.extractfile(member).read())
    repo = root / "v3-toy-repo"
    make_repo(repo, files={"README.txt": "v3 migration fixture\n"})
    db_path = root / "live-v3-home" / "labops.db"
    db_path.parent.mkdir(parents=True)
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from labops.db import connect, migrate\n"
        "from labops.tasks import add_repo, create_task, set_confidential\n"
        "repo, db_path = Path(sys.argv[1]), Path(sys.argv[2])\n"
        "conn = connect(db_path)\n"
        "migrate(conn)\n"
        "add_repo(conn, repo_id='toy-v3', path=repo)\n"
        "set_confidential(conn, 'toy-v3', True)\n"
        "create_task(conn, repo_id='toy-v3', task_id='T20261020-v3-copy', goal='v3 fixture', profile={'schema': 1})\n"
        "conn.close()\n"
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run([str(_VENV_PYTHON), "-c", script, str(repo), str(db_path)],
                            cwd=legacy_src, capture_output=True, text=True, env=env)
    if result.returncode:
        raise AssertionError(f"v3 fixture failed: {result.stderr}\n{result.stdout}")
    copied = root / "live-v3-copy.db"
    shutil.copy2(db_path, copied)
    return copied


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables}


def _signature(conn):
    return (
        conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],
        _row_counts(conn),
    )


def _export(conn, root, output):
    old = _signature(conn)
    sql = "\n".join(conn.iterdump()) + "\n"
    # Strip the generation machine's temp root, including JSON-escaped versions.
    for original in (str(root), root.as_posix()):
        for value in (json_escape(original), original):
            sql = re.sub(re.escape(value), "fixture-root", sql, flags=re.IGNORECASE)
    with closing(sqlite3.connect(":memory:")) as restored:
        restored.executescript(sql)
        new = _signature(restored)
    if old != new:
        raise RuntimeError(f"{output.name}: historical and restored signatures differ: {old!r}, {new!r}")
    output.write_text(sql, encoding="utf-8", newline="\n")
    print(f"{output.name}: schema_version={old[0]}, old={old[1]!r}, fixture={new[1]!r}")


def json_escape(value):
    import json
    return json.dumps(value, ensure_ascii=False)[1:-1]


def main():
    fixtures = ROOT / "tests" / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="labops-legacy-") as directory:
        root = Path(directory)
        v1, _ = _make_m0a_v1_db(root)
        with closing(v1):
            _export(v1, root, fixtures / "legacy_v1.sql")
        for build, name in ((_make_v2_db, "legacy_v2.sql"), (_make_v3_db_copy, "legacy_v3.sql")):
            with closing(sqlite3.connect(build(root))) as conn:
                _export(conn, root, fixtures / name)


if __name__ == "__main__":
    main()
