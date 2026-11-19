import os
import re
import shutil
import subprocess
from pathlib import Path

from .errors import GitConflict, ValidationError

_HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _git_path() -> str:
    path = shutil.which("git")
    if path is None:
        raise ValidationError("git_not_found")
    return path


def _env(*, read_only: bool) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    if read_only:
        env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def run(repo: Path, *args: str, read_only: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    cmd = [_git_path(), "-C", str(repo), "-c", "core.quotepath=false", *map(str, args)]
    result = subprocess.run(cmd, capture_output=True, env=_env(read_only=read_only), shell=False)
    if check and result.returncode:
        stderr = result.stderr.decode("utf-8", "surrogateescape").strip()
        raise ValidationError("git_command_failed", detail={"args": list(args), "returncode": result.returncode, "stderr": stderr})
    return result


def _text(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape").strip()


def object_format(repo: Path) -> str:
    value = _text(run(repo, "rev-parse", "--show-object-format").stdout)
    if value not in {"sha1", "sha256"}:
        raise ValidationError("unsupported_object_format", detail={"format": value})
    return value


def zero_oid(repo: Path) -> str:
    return "0" * (40 if object_format(repo) == "sha1" else 64)


def rev_parse(repo: Path, rev: str) -> str | None:
    result = run(repo, "rev-parse", "--verify", rev, read_only=True, check=False)
    return _text(result.stdout) if result.returncode == 0 else None


def object_type(repo: Path, oid: str) -> str | None:
    result = run(repo, "cat-file", "-t", oid, read_only=True, check=False)
    return _text(result.stdout) if result.returncode == 0 else None


def tree_of(repo: Path, commit: str) -> str:
    result = rev_parse(repo, f"{commit}^{{tree}}")
    if result is None:
        raise ValidationError("tree_not_found", detail={"commit": commit})
    return result


def is_ancestor(repo: Path, a: str, b: str) -> bool:
    result = run(repo, "merge-base", "--is-ancestor", a, b, read_only=True, check=False)
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise ValidationError("git_ancestor_check_failed", detail={"a": a, "b": b, "returncode": result.returncode})


def read_ref(repo: Path, ref: str) -> str | None:
    result = run(repo, "rev-parse", "--verify", "-q", ref, read_only=True, check=False)
    return _text(result.stdout) if result.returncode == 0 else None


def update_ref_cas(repo: Path, ref: str, new: str, expected_old: str) -> bool:
    return run(repo, "update-ref", ref, new, expected_old, read_only=False, check=False).returncode == 0


def for_each_ref(repo: Path, prefix: str) -> list[tuple[str, str]]:
    data = run(repo, "for-each-ref", "--format=%(refname)%00%(objectname)", prefix).stdout
    result = []
    for line in data.splitlines():
        fields = line.split(b"\0", 1)
        if len(fields) == 2:
            result.append((_text(fields[0]), _text(fields[1])))
    return result


def worktrees(repo: Path) -> list[dict]:
    data = run(repo, "worktree", "list", "--porcelain", "-z").stdout
    records: list[dict] = []
    current: dict = {}
    for raw in data.split(b"\0"):
        if not raw:
            if current:
                records.append(current)
                current = {}
            continue
        key, sep, value = raw.partition(b" ")
        name = key.decode("ascii", "replace")
        decoded = value.decode("utf-8", "surrogateescape") if sep else ""
        if name == "worktree":
            current["path"] = decoded
        elif name == "HEAD":
            current["head"] = decoded
        elif name == "branch":
            current["branch"] = decoded
        elif name == "detached":
            current["detached"] = True
        elif name == "locked":
            current["locked"] = decoded or True
        elif name == "prunable":
            current["prunable"] = decoded or True
    if current:
        records.append(current)
    for item in records:
        item.setdefault("head", None)
        item.setdefault("branch", None)
        item.setdefault("detached", False)
        item.setdefault("locked", False)
        item.setdefault("prunable", False)
    return records


def status_porcelain(repo_or_wt: Path, untracked: str = "all") -> list[tuple[str, str]]:
    data = run(repo_or_wt, "status", "--porcelain=v1", "-z", "--no-renames", f"--untracked-files={untracked}").stdout
    result = []
    for record in data.split(b"\0"):
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise ValidationError("invalid_status_porcelain", detail={"record": record.decode("utf-8", "surrogateescape")})
        result.append((record[:2].decode("ascii", "replace"), record[3:].decode("utf-8", "surrogateescape")))
    return result


def diff_name_status(repo: Path, a: str, b: str) -> list[tuple[str, str]]:
    data = run(repo, "diff", "--no-renames", "--name-status", "-z", a, b).stdout
    fields = data.split(b"\0")
    result = []
    i = 0
    while i + 1 < len(fields):
        if fields[i]:
            status = fields[i].decode("ascii", "replace")
            path = fields[i + 1].decode("utf-8", "surrogateescape")
            result.append((status, path))
        i += 2
    return result


def diff_numstat(repo: Path, a: str, b: str) -> list[tuple[int | None, int | None, str]]:
    data = run(repo, "diff", "--no-renames", "--numstat", "-z", a, b).stdout
    result = []
    for record in data.split(b"\0"):
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            raise ValidationError("invalid_numstat", detail={"record": record.decode("utf-8", "surrogateescape")})
        added = None if fields[0] == b"-" else int(fields[0])
        deleted = None if fields[1] == b"-" else int(fields[1])
        result.append((added, deleted, fields[2].decode("utf-8", "surrogateescape")))
    return result


def diff_hunks(repo: Path, a: str, b: str, path: str) -> list[tuple[int, int, int, int]]:
    data = run(repo, "diff", "--no-ext-diff", "--no-renames", "-U0", a, b, "--", path).stdout
    result = []
    for line in data.splitlines():
        match = _HUNK_RE.match(line)
        if match:
            old_start, old_len, new_start, new_len = match.groups()
            result.append((int(old_start), int(old_len or b"1"), int(new_start), int(new_len or b"1")))
    return result


def show_file(repo: Path, commit: str, path: str) -> bytes | None:
    result = run(repo, "show", f"{commit}:{path}", read_only=True, check=False)
    return result.stdout if result.returncode == 0 else None


def commit_trailers(repo: Path, commit: str) -> dict[str, str]:
    data = run(repo, "log", "-1", "--format=%(trailers:only,unfold)", commit).stdout
    result: dict[str, str] = {}
    for line in data.splitlines():
        key, sep, value = line.partition(b":")
        if sep:
            result[key.decode("utf-8", "surrogateescape").strip()] = value.decode("utf-8", "surrogateescape").strip()
    return result
