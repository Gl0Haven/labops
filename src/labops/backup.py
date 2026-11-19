import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import gitio
from .config import now
from .db import tx
from .errors import ValidationError
from .ids import new_id
from .tasks import repo_row


_REF_PREFIXES = ("refs/heads", "refs/tags", "refs/labops")


def _backup_directory(repo) -> tuple[Path, Path]:
    if not repo["backup_root"]:
        raise ValidationError("backup_root_not_configured", detail={"repo_id": repo["repo_id"]})
    backup_root = Path(repo["backup_root"]).expanduser()
    if not backup_root.is_absolute():
        raise ValidationError("backup_root_must_be_absolute", detail={"path": str(backup_root)})
    backup_root = backup_root.resolve()
    repo_path = Path(repo["path"]).resolve()
    try:
        backup_root.relative_to(repo_path)
    except ValueError:
        pass
    else:
        raise ValidationError("backup_root_inside_repo", detail={"backup_root": str(backup_root)})
    return backup_root, backup_root / repo["repo_id"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    executable = shutil.which("git")
    if executable is None:
        raise ValidationError("git_not_found")
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    result = subprocess.run([executable, *args], cwd=cwd, capture_output=True, env=env, shell=False)
    return result


def _raise_git_error(action: str, result: subprocess.CompletedProcess) -> None:
    raise ValidationError("backup_git_command_failed", detail={
        "action": action,
        "returncode": result.returncode,
        "stderr": result.stderr.decode("utf-8", "surrogateescape").strip(),
    })


def _registered_backup_is_inside(path: Path, directory: Path) -> bool:
    try:
        resolved = path.resolve()
        resolved.relative_to(directory.resolve())
        return resolved.parent == directory.resolve()
    except (OSError, ValueError):
        return False


def _rotate(conn: sqlite3.Connection, repo, directory: Path, keep: int) -> list[str]:
    rows = conn.execute(
        "SELECT backup_id,path,created_at FROM backups WHERE repo_id=? ORDER BY created_at DESC,rowid DESC",
        (repo["repo_id"],),
    ).fetchall()
    removed = []
    for row in rows[keep:]:
        path = Path(row["path"])
        if not _registered_backup_is_inside(path, directory):
            continue
        if path.exists():
            path.unlink()
        with tx(conn):
            conn.execute("DELETE FROM backups WHERE backup_id=?", (row["backup_id"],))
        removed.append(str(path))
    return removed


def run_backup(conn: sqlite3.Connection, repo_id: str) -> dict:
    repo = repo_row(conn, repo_id)
    backup_root, directory = _backup_directory(repo)
    if not Path(repo["path"]).is_dir():
        raise ValidationError("repo_path_missing", detail={"repo_id": repo_id})
    keep = repo["backup_keep"]
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValidationError("invalid_backup_keep", detail={"backup_keep": keep})
    directory.mkdir(parents=True, exist_ok=True)
    backup_id = new_id("bk")
    stamp = datetime.now(timezone.utc).replace(microsecond=0)
    final_path = directory / f"{repo_id}-{stamp.strftime('%Y%m%dT%H%M%SZ')}.bundle"
    while final_path.exists():
        stamp += timedelta(seconds=1)
        final_path = directory / f"{repo_id}-{stamp.strftime('%Y%m%dT%H%M%SZ')}.bundle"
    temporary_path = directory / f".{backup_id}.partial"
    repo_path = Path(repo["path"])
    try:
        created = gitio.run(repo_path, "bundle", "create", str(temporary_path), "--all",
                            read_only=False, check=False)
        if created.returncode:
            _raise_git_error("bundle_create", created)
        verified = gitio.run(repo_path, "bundle", "verify", str(temporary_path),
                             read_only=False, check=False)
        if verified.returncode:
            _raise_git_error("bundle_verify", verified)
        os.replace(temporary_path, final_path)
    except BaseException:
        if temporary_path.exists():
            temporary_path.unlink()
        raise
    head = gitio.rev_parse(repo_path, "HEAD")
    ref_count = sum(len(gitio.for_each_ref(repo_path, prefix)) for prefix in _REF_PREFIXES)
    digest = _sha256(final_path)
    created_at = now()
    try:
        with tx(conn):
            conn.execute(
                "INSERT INTO backups(backup_id,repo_id,path,head,ref_count,sha256,verified,created_at) "
                "VALUES(?,?,?,?,?,?,1,?)",
                (backup_id, repo_id, str(final_path), head, ref_count, digest, created_at),
            )
    except BaseException:
        # The final bundle was created by this call and is safe to remove if its row cannot be registered.
        if final_path.exists():
            final_path.unlink()
        raise
    removed = _rotate(conn, repo, directory, keep)
    return {"backup_id": backup_id, "repo_id": repo_id, "path": str(final_path), "head": head,
            "ref_count": ref_count, "sha256": digest, "verified": True, "rotated": removed}


def list_backups(conn: sqlite3.Connection, repo_id: str) -> list[dict]:
    repo_row(conn, repo_id)
    return [dict(row) for row in conn.execute(
        "SELECT backup_id,repo_id,path,head,ref_count,sha256,verified,created_at FROM backups "
        "WHERE repo_id=? ORDER BY created_at DESC,rowid DESC", (repo_id,)
    ).fetchall()]


def _ref_map(repo_path: Path) -> dict[str, str]:
    refs = {}
    for prefix in _REF_PREFIXES:
        refs.update(dict(gitio.for_each_ref(repo_path, prefix)))
    return refs


def verify_restore(conn: sqlite3.Connection, repo_id: str, *, backup_id: str | None = None) -> dict:
    repo = repo_row(conn, repo_id)
    _backup_root, directory = _backup_directory(repo)
    if backup_id is None:
        row = conn.execute(
            "SELECT * FROM backups WHERE repo_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (repo_id,)
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM backups WHERE repo_id=? AND backup_id=?", (repo_id, backup_id)).fetchone()
    if row is None:
        raise ValidationError("backup_not_found", detail={"repo_id": repo_id, "backup_id": backup_id})
    bundle_path = Path(row["path"])
    if not _registered_backup_is_inside(bundle_path, directory) or not bundle_path.is_file():
        raise ValidationError("backup_file_invalid", detail={"backup_id": row["backup_id"]})
    if _sha256(bundle_path) != row["sha256"]:
        raise ValidationError("backup_checksum_mismatch", detail={"backup_id": row["backup_id"]})
    with tempfile.TemporaryDirectory(prefix="labops-restore-") as temporary:
        mirror_path = Path(temporary) / "mirror.git"
        cloned = _git_run(["clone", "--mirror", str(bundle_path), str(mirror_path)])
        if cloned.returncode:
            _raise_git_error("clone", cloned)
        source_refs = _ref_map(Path(repo["path"]))
        restored_refs = _ref_map(mirror_path)
        missing = sorted(name for name, oid in source_refs.items() if restored_refs.get(name) is None)
        mismatched = [
            {"ref": name, "expected": oid, "actual": restored_refs[name]}
            for name, oid in source_refs.items()
            if name in restored_refs and restored_refs[name] != oid
        ]
        mismatched.extend(
            {"ref": name, "expected": None, "actual": oid}
            for name, oid in restored_refs.items() if name not in source_refs
        )
    result = {"backup_id": row["backup_id"], "ok": not missing and not mismatched,
              "missing": missing, "mismatched": mismatched}
    record = {"repo_id": repo_id, "backup_id": row["backup_id"], "ok": result["ok"], "at": now(),
              "missing_count": len(missing), "mismatched_count": len(mismatched)}
    with tx(conn):
        conn.execute("INSERT INTO meta(key,value) VALUES('last_verify_restore',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(record, sort_keys=True),))
    return result
