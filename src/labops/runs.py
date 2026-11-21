"""Read-only ingestion of Codex background-run metadata and progress logs."""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from .canon import canonical_json
from .config import now
from .db import tx
from .errors import ValidationError

_RUN_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_PROGRESS_RE = re.compile(
    r"(?:\d+/\d+|epoch\s*\d+|step\s*\d+|iter\w*\s*\d+|\d+%\|)", re.IGNORECASE
)
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_TERMINAL = {"completed", "failed", "stopped", "launch_incomplete"}
_EVENT_FOR_STATUS = {
    "completed": "run_completed",
    "failed": "run_failed",
    "launch_incomplete": "run_failed",
    "stopped": "run_stopped",
}
_TAIL_BYTES = 64 * 1024
_WATCH_FILES_PER_GLOB = 5000
_PROGRESS_RULE_JSON_BYTES = 1024 * 1024
_PROGRESS_TEMPLATE_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ProcessInfoUnavailable(RuntimeError):
    """The process query could not run reliably; callers must avoid marking it exited."""


def _process_info(pid: int) -> dict | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValidationError("invalid_process_pid", detail={"pid": pid})
    pwsh = shutil.which("pwsh")
    if not pwsh:
        raise ProcessInfoUnavailable("pwsh_not_found")
    script = f"""
$targetPid = {pid}
try {{
    $proc = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
    if ($null -eq $proc) {{ [Console]::Out.WriteLine('null'); exit 0 }}
    $native = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $targetPid" -ErrorAction Stop
    if ($null -eq $native) {{ [Console]::Out.WriteLine('null'); exit 0 }}
    $value = [ordered]@{{
        id = [int]$proc.Id
        start_utc = $proc.StartTime.ToUniversalTime().ToString('o')
        command_line = $native.CommandLine
    }}
    ConvertTo-Json -InputObject $value -Compress -Depth 3
}} catch {{
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 2
}}
"""
    try:
        result = subprocess.run(
            [pwsh, "-NoProfile", "-Command", script], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, check=False, shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProcessInfoUnavailable("process_query_timeout") from exc
    except OSError as exc:
        raise ProcessInfoUnavailable("process_query_unavailable") from exc
    if result.returncode != 0:
        error = (result.stderr or "").strip().splitlines()
        detail = error[-1][:200] if error else "process_query_failed"
        raise ProcessInfoUnavailable(f"process_query_failed: {detail}")
    try:
        value = json.loads(result.stdout.strip())
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProcessInfoUnavailable("process_query_invalid_json") from exc
    if value is None:
        return None
    if (not isinstance(value, dict) or isinstance(value.get("id"), bool)
            or value.get("id") != pid or not isinstance(value.get("start_utc"), str)
            or value.get("command_line") is not None and not isinstance(value.get("command_line"), str)):
        raise ProcessInfoUnavailable("process_query_invalid_shape")
    return value


def _normalize_process_started_at(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("invalid_process_started_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("invalid_process_started_at") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _process_identity_matches(pid: int | None, started_at: str | None, process: dict | None) -> bool:
    if process is None or isinstance(pid, bool) or not isinstance(pid, int):
        return False
    if (not isinstance(process, dict) or process.get("id") != pid
            or not isinstance(process.get("start_utc"), str) or not isinstance(started_at, str)):
        return False
    try:
        expected = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        observed = datetime.fromisoformat(process["start_utc"].replace("Z", "+00:00"))
        if expected.tzinfo is None:
            expected = expected.replace(tzinfo=timezone.utc)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return abs((observed - expected).total_seconds()) <= 2.0
    except (KeyError, TypeError, ValueError):
        return False


def _script_from_command_line(command_line: str | None) -> str | None:
    if not isinstance(command_line, str) or not command_line.strip():
        return None
    try:
        arguments = shlex.split(command_line, posix=False)
    except ValueError:
        arguments = command_line.split()
    for argument in arguments:
        candidate = argument.strip().strip('"')
        if candidate.casefold().endswith((".py", ".ps1")):
            return candidate
    return None


def _state_root(state_root: str | Path | None) -> Path:
    if state_root is not None:
        return Path(state_root).expanduser()
    override = os.environ.get("LABOPS_BG_STATE_ROOT")
    if override:
        return Path(override).expanduser()
    profile = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(profile) / ".codex" / "background-state"


def _normal_path(value: str) -> str:
    # Windows paths can also be recorded with forward slashes. Do not resolve the
    # path: the record may refer to a completed or removed working directory.
    return os.path.normcase(os.path.normpath(value.replace("/", "\\"))).rstrip("\\/").casefold()


def _repo_for_working_directory(conn: sqlite3.Connection, working_directory: str):
    candidate = _normal_path(working_directory)
    for repo in conn.execute("SELECT repo_id,path FROM repos ORDER BY length(path) DESC,repo_id"):
        base = _normal_path(repo["path"])
        if candidate == base or candidate.startswith(base + "\\") or candidate.startswith(base + "/"):
            return repo["repo_id"], repo["path"]
    return None, None


def _read_json(path: Path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected_json_object")
    return value


def _valid_timestamp(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid_timestamp")
    # Validate while preserving the source format for API compatibility.
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _log_paths(run_dir: str | None) -> list[Path]:
    if not run_dir:
        return []
    root = Path(run_dir)
    return [root / "output.log", root / "error.log"]


def _read_tail(path: Path) -> tuple[str, str | None]:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - _TAIL_BYTES), os.SEEK_SET)
            content = stream.read(_TAIL_BYTES).decode("utf-8", errors="replace")
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
        return content, modified
    except OSError:
        return "", None


def _timestamp_order(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (AttributeError, TypeError, ValueError):
        return float("-inf")


def _latest_log_data(run_dir: str | None, *, extra_paths: list[str | Path] | None = None
                     ) -> tuple[str | None, str | None, str | None, str | None]:
    tails = []
    mtimes = []
    error_tail = ""
    paths = _log_paths(run_dir)
    seen_paths = {os.path.normcase(os.path.abspath(str(path))) for path in paths}
    for value in extra_paths or []:
        path = Path(value)
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in seen_paths:
            paths.append(path)
            seen_paths.add(key)
    for path in paths:
        content, modified = _read_tail(path)
        tails.append((modified or "", content))
        if modified:
            mtimes.append(modified)
        if path.name == "error.log":
            error_tail = content
    log_mtime = max(mtimes, key=_timestamp_order) if mtimes else None
    progress_hint = None
    progress_mtime = None
    for modified, content in sorted(tails, key=lambda item: _timestamp_order(item[0]), reverse=True):
        lines = content.replace("\r", "\n").split("\n")
        for line in reversed(lines):
            clean = _ANSI_RE.sub("", line).strip()
            if clean and _PROGRESS_RE.search(clean):
                progress_hint = clean[:160]
                progress_mtime = modified or None
                break
        if progress_hint is not None:
            break
    error_lines = [_ANSI_RE.sub("", line).strip() for line in error_tail.replace("\r", "\n").split("\n")]
    error_line = next((line[:100] for line in reversed(error_lines) if line), None)
    return progress_hint, progress_mtime, log_mtime, error_line


def validate_progress_rules(rules: list[dict]) -> list[dict]:
    """Validate and normalize the JSON objects supplied through repo set."""
    if not isinstance(rules, list):
        raise ValidationError("invalid_progress_rules")
    if len(rules) > 8:
        raise ValidationError("too_many_progress_rules", detail={"maximum": 8})
    normalized = []
    allowed = {"name", "glob", "type", "path_regex", "pattern", "template"}
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - allowed:
            raise ValidationError("invalid_progress_rule")
        name = rule.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("invalid_progress_rule_name")
        rule_type = rule.get("type")
        if not isinstance(rule_type, str) or rule_type not in {"json", "line_regex"}:
            raise ValidationError("invalid_progress_rule_type", detail={"name": name})
        glob = rule.get("glob")
        if not isinstance(glob, str):
            raise ValidationError("invalid_progress_rule_glob", detail={"name": name})
        glob = glob.strip().replace("\\", "/")
        windows_glob = PureWindowsPath(glob)
        posix_glob = PurePosixPath(glob)
        if (not glob or "\x00" in glob or windows_glob.is_absolute() or windows_glob.drive
                or posix_glob.is_absolute() or ".." in posix_glob.parts):
            raise ValidationError("invalid_progress_rule_glob", detail={"name": name, "glob": glob})
        template = rule.get("template")
        if not isinstance(template, str) or not 1 <= len(template) <= 200:
            raise ValidationError("invalid_progress_rule_template", detail={"name": name})
        item = {"name": name.strip(), "glob": glob, "type": rule_type, "template": template}
        path_regex = rule.get("path_regex")
        if path_regex is not None:
            if not isinstance(path_regex, str):
                raise ValidationError("invalid_progress_rule_path_regex", detail={"name": name})
            try:
                re.compile(path_regex)
            except re.error as exc:
                raise ValidationError("invalid_progress_rule_path_regex", detail={"name": name}) from exc
            item["path_regex"] = path_regex
        pattern = rule.get("pattern")
        if rule_type == "line_regex" or pattern is not None:
            if not isinstance(pattern, str) or not pattern:
                raise ValidationError("invalid_progress_rule_pattern", detail={"name": name})
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValidationError("invalid_progress_rule_pattern", detail={"name": name}) from exc
            item["pattern"] = pattern
        normalized.append(item)
    return normalized


def parse_progress_rules(values: list[str] | str | None) -> list[dict]:
    """Decode repeated --progress-rule JSON values; one empty value clears rules."""
    if values is None:
        raise ValidationError("repo_options_required")
    if isinstance(values, str):
        items = [values]
    elif isinstance(values, list):
        items = list(values)
    else:
        raise ValidationError("invalid_progress_rule_json")
    if not items or items == [""]:
        return []
    parsed = []
    for value in items:
        if not isinstance(value, str):
            raise ValidationError("invalid_progress_rule_json")
        try:
            parsed.append(json.loads(value))
        except json.JSONDecodeError as exc:
            raise ValidationError("invalid_progress_rule_json") from exc
    return validate_progress_rules(parsed)


def _progress_timestamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")


def _format_progress_value(value) -> str:
    if value is None:
        return "?"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return format(value, ".3g")
    if isinstance(value, (int, str)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _render_progress_template(template: str, fields: dict) -> str:
    rendered = _PROGRESS_TEMPLATE_RE.sub(
        lambda match: _format_progress_value(fields.get(match.group(1))), template
    )
    return _ANSI_RE.sub("", rendered)[:160]


def _evaluate_progress_rule(repo_path: str, rule: dict) -> dict:
    root = Path(repo_path).resolve()
    paths, truncated = _watch_paths(str(root), [rule["glob"]])
    if truncated:
        raise ValueError("too_many_files")
    latest = None
    latest_mtime = None
    latest_relative = None
    for path in paths:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        modified = _progress_timestamp(path)
        relative = path.relative_to(root).as_posix()
        if (latest_mtime is None or _timestamp_order(modified) > _timestamp_order(latest_mtime)
                or (_timestamp_order(modified) == _timestamp_order(latest_mtime)
                    and relative > latest_relative)):
            latest, latest_mtime, latest_relative = path, modified, relative
    if latest is None:
        return {"name": rule["name"], "glob": rule["glob"], "file": None,
                "mtime": None, "fields": {}, "progress_hint": None}

    fields = {}
    if rule.get("path_regex"):
        match = re.search(rule["path_regex"], latest_relative)
        if match:
            fields.update({key: value for key, value in match.groupdict().items() if value is not None})
    if rule["type"] == "json":
        if latest.stat().st_size > _PROGRESS_RULE_JSON_BYTES:
            raise ValueError("file_too_large")
        with latest.open("rb") as stream:
            content = stream.read(_PROGRESS_RULE_JSON_BYTES)
        payload = json.loads(content.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("json_not_object")
        fields.update(payload)
    else:
        with latest.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - _TAIL_BYTES), os.SEEK_SET)
            content = stream.read(_TAIL_BYTES).decode("utf-8", errors="replace")
        pattern = re.compile(rule["pattern"])
        for line in reversed(content.replace("\r", "\n").split("\n")):
            match = pattern.search(line)
            if match:
                fields.update({key: value for key, value in match.groupdict().items() if value is not None})
                break
        else:
            return {"name": rule["name"], "glob": rule["glob"], "file": latest_relative,
                    "mtime": latest_mtime, "fields": fields, "progress_hint": None}
    return {"name": rule["name"], "glob": rule["glob"], "file": latest_relative,
            "mtime": latest_mtime, "fields": fields,
            "progress_hint": _render_progress_template(rule["template"], fields)}


def _progress_rule_results(repo_path: str, raw_rules) -> tuple[list[dict], int]:
    if not raw_rules:
        return [], 0
    try:
        stored = json.loads(raw_rules) if isinstance(raw_rules, str) else raw_rules
        if not isinstance(stored, list) or len(stored) > 8:
            raise ValueError("invalid_rule_list")
    except (TypeError, ValueError, json.JSONDecodeError):
        return [{"name": "?", "glob": None, "file": None, "mtime": None,
                 "fields": {}, "progress_hint": None, "error": "invalid_rule_list"}], 1
    results = []
    errors = 0
    for raw_rule in stored:
        try:
            rule = validate_progress_rules([raw_rule])[0]
            result = _evaluate_progress_rule(repo_path, rule)
        except (OSError, RuntimeError, TypeError, ValueError, re.error, ValidationError) as exc:
            errors += 1
            name = raw_rule.get("name", "?") if isinstance(raw_rule, dict) else "?"
            result = {"name": name, "glob": raw_rule.get("glob") if isinstance(raw_rule, dict) else None,
                      "file": None, "mtime": None, "fields": {}, "progress_hint": None,
                      "error": str(exc)[:120] or "progress_rule_error"}
        results.append(result)
    return results, errors


def preview_progress_rules(conn: sqlite3.Connection, repo_id: str) -> dict:
    row = conn.execute("SELECT repo_id,path,run_progress_rules FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
    if row is None:
        raise ValidationError("repo_not_found", detail={"repo_id": repo_id})
    results, errors = _progress_rule_results(row["path"], row["run_progress_rules"])
    return {"repo_id": repo_id, "rules": results, "progress_rule_errors": errors}


def _best_progress_hint(log_hint: str | None, log_hint_mtime: str | None,
                        rule_results: list[dict]) -> str | None:
    candidates = []
    if log_hint is not None and log_hint_mtime is not None:
        candidates.append((log_hint, log_hint_mtime))
    candidates.extend((item["progress_hint"], item["mtime"]) for item in rule_results
                      if item.get("progress_hint") is not None and item.get("mtime") is not None)
    return max(candidates, key=lambda item: _timestamp_order(item[1]))[0] if candidates else None


def _latest_timestamp(*values: str | None) -> str | None:
    candidates = [value for value in values if value is not None]
    return max(candidates, key=_timestamp_order) if candidates else None


def _watch_globs(raw_value) -> list[str]:
    if not raw_value:
        return []
    try:
        patterns = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(patterns, list):
        return []
    return [item for item in patterns if isinstance(item, str) and item]


def _watch_paths(repo_path: str, patterns: list[str]) -> tuple[list[Path], int]:
    root = Path(repo_path)
    paths = []
    truncated = 0
    seen_paths = set()
    for pattern in patterns:
        files_seen = 0
        try:
            matches = root.glob(pattern)
            for match in matches:
                try:
                    if not match.is_file():
                        continue
                    if files_seen >= _WATCH_FILES_PER_GLOB:
                        truncated += 1
                        break
                    files_seen += 1
                    key = os.path.normcase(os.path.abspath(str(match)))
                    if key not in seen_paths:
                        paths.append(match)
                        seen_paths.add(key)
                except OSError:
                    continue
        except (OSError, ValueError, NotImplementedError):
            continue
    return paths, truncated


def _watch_mtime(repo_path: str, patterns: list[str]) -> tuple[str | None, int]:
    latest = None
    paths, truncated = _watch_paths(repo_path, patterns)
    for path in paths:
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
            if latest is None or _timestamp_order(modified) > _timestamp_order(latest):
                latest = modified
        except OSError:
            continue
    return latest, truncated


def _age_seconds(timestamp: str, now_ts: str) -> float | None:
    try:
        observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        current = datetime.fromisoformat(now_ts.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return max(0.0, (current - observed).total_seconds())
    except (TypeError, ValueError):
        return None


def _stall_minutes(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='runs_stall_minutes'").fetchone()
    try:
        value = int(row[0]) if row else 60
    except (TypeError, ValueError):
        return 60
    return value if 1 <= value <= 7 * 24 * 60 else 60


def _event(conn: sqlite3.Connection, *, key: str, timestamp: str, before: str | None,
           state: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) "
        "VALUES(?,?,?,?,?,'labops',?)",
        (timestamp, "bg_run", key, before, state,
         json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
    )


def _load_records(root: Path) -> tuple[list[dict], int, int]:
    records: list[dict] = []
    skipped = 0
    seen = 0
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return records, seen, skipped
    for directory in children:
        if not directory.is_dir() or not _RUN_KEY_RE.fullmatch(directory.name):
            continue
        try:
            launch = _read_json(directory / "launch.json")
            process = _read_json(directory / "process.json")
            status = _read_json(directory / "status.json")
            working = launch.get("workingDirectory")
            if not isinstance(working, str) or not working.strip():
                raise ValueError("invalid_working_directory")
            run_status = status.get("status")
            if run_status not in {"running", "completed", "failed", "stopped", "launch_incomplete"}:
                raise ValueError("invalid_status")
            process_pid = process.get("pid")
            if isinstance(process_pid, bool) or not isinstance(process_pid, int):
                raise ValueError("invalid_process_pid")
            process_started = _valid_timestamp(process.get("processStartedAtUtc"))
            if process_started is None:
                raise ValueError("missing_process_started_at")
            process_run_dir = process.get("runDirectory")
            process_log_path = process.get("logPath")
            if not isinstance(process_run_dir, str) or not process_run_dir.strip():
                raise ValueError("invalid_process_run_directory")
            if not isinstance(process_log_path, str) or not process_log_path.strip():
                raise ValueError("invalid_process_log_path")
            pid = status.get("pid", process.get("pid"))
            if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int)):
                raise ValueError("invalid_pid")
            run_dir = launch.get("runDirectory") or process_run_dir
            if run_dir is None:
                run_dir = str(Path(process_log_path).parent)
            if run_dir is not None and (not isinstance(run_dir, str) or not run_dir.strip()):
                raise ValueError("invalid_run_directory")
            script_path = launch.get("scriptPath")
            if not isinstance(script_path, str) or not script_path.strip():
                raise ValueError("invalid_script_path")
            started_at = _valid_timestamp(status.get("startedAtUtc"))
            finished_at = _valid_timestamp(status.get("finishedAtUtc"))
            exit_code = status.get("exitCode")
            if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
                raise ValueError("invalid_exit_code")
            seen += 1
            records.append({
                "run_key": directory.name.casefold(),
                "working_directory": working,
                "run_dir": run_dir or "",
                "script_path": script_path,
                "status": run_status,
                "pid": pid,
                "started_at": started_at,
                "finished_at": finished_at,
                "exit_code": exit_code,
            })
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            skipped += 1
    return records, seen, skipped


def _validate_run_watch_globs(values: list[str] | None) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValidationError("invalid_run_watch_globs")
    patterns = []
    for value in values:
        pattern = value.strip().replace("\\", "/")
        windows_pattern = PureWindowsPath(value)
        posix_pattern = PurePosixPath(pattern)
        if (not pattern or windows_pattern.is_absolute() or windows_pattern.drive
                or posix_pattern.is_absolute() or ".." in posix_pattern.parts or "\x00" in pattern):
            raise ValidationError("invalid_run_watch_glob", detail={"pattern": value})
        if pattern not in patterns:
            patterns.append(pattern)
    return patterns


def _adopted_run_for_identity(conn: sqlite3.Connection, pid: int, process_started_at: str):
    return conn.execute(
        "SELECT * FROM bg_runs WHERE source='adopted' AND pid=? AND process_started_at=? "
        "ORDER BY run_key LIMIT 1", (pid, process_started_at),
    ).fetchone()


def adopt_process(conn: sqlite3.Connection, *, repo_id: str, pid: int, title: str,
                  log_path: str | None = None, watch_globs: list[str] | None = None,
                  actor: str) -> dict:
    if actor not in {"codex", "opus", "gemini", "cli"}:
        raise ValidationError("adopt_actor_not_allowed", detail={"actor": actor})
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValidationError("invalid_process_pid", detail={"pid": pid})
    repo = conn.execute("SELECT repo_id,path,run_watch_globs FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
    if repo is None:
        raise ValidationError("repo_not_found", detail={"repo_id": repo_id})
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
        raise ValidationError("invalid_adopt_title")
    try:
        process = _process_info(pid)
    except ProcessInfoUnavailable as exc:
        raise ValidationError("process_query_failed", detail={"reason": str(exc)}) from exc
    if process is None:
        raise ValidationError("process_not_found", detail={"pid": pid})
    if process.get("id") != pid or not isinstance(process.get("start_utc"), str):
        raise ValidationError("process_info_invalid", detail={"pid": pid})
    process_started_at = _normalize_process_started_at(process["start_utc"])
    existing = _adopted_run_for_identity(conn, pid, process_started_at)
    if existing is not None:
        return {"run": dict(existing), "replayed": True}

    repo_path = Path(repo["path"]).resolve()
    resolved_log_path = None
    if log_path is not None:
        if not isinstance(log_path, str) or not log_path.strip():
            raise ValidationError("invalid_adopt_log_path")
        candidate = Path(log_path).expanduser()
        if not candidate.is_absolute():
            candidate = repo_path / candidate
        try:
            resolved_log_path = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValidationError("adopt_log_path_missing", detail={"log_path": log_path}) from exc
        try:
            resolved_log_path.relative_to(repo_path)
        except ValueError as exc:
            raise ValidationError("adopt_log_path_outside_repo", detail={"log_path": log_path}) from exc
        if not resolved_log_path.is_file():
            raise ValidationError("adopt_log_path_not_file", detail={"log_path": log_path})
    normalized_globs = _validate_run_watch_globs(watch_globs)
    timestamp = now()
    run_key = f"adopt_{pid}_{int(datetime.fromisoformat(process_started_at.replace('Z', '+00:00')).timestamp())}"
    command_line = process.get("command_line")
    if command_line is not None:
        command_line = command_line[:400]
    script_path = _script_from_command_line(process.get("command_line"))
    run_dir = str(resolved_log_path.parent if resolved_log_path is not None else repo_path)
    watch_json = canonical_json(normalized_globs).decode("utf-8")
    detail = {
        "source": "adopted", "title": title.strip(), "pid": pid,
        "process_started_at": process_started_at,
        "script_path": script_path,
    }
    try:
        with tx(conn):
            existing = _adopted_run_for_identity(conn, pid, process_started_at)
            if existing is not None:
                return {"run": dict(existing), "replayed": True}
            conn.execute(
                "INSERT INTO bg_runs(run_key,repo_id,run_dir,script_path,status,pid,started_at,finished_at,"
                "exit_code,progress_hint,progress_at,log_mtime,last_notified_status,stall_notified,orphaned,"
                "source,process_started_at,command_line,title,log_path,watch_globs,updated_at) "
                "VALUES(?,?,?,?,'running',?,?,NULL,NULL,NULL,NULL,NULL,'running',0,0,'adopted',?,?,?,?,?,?)",
                (run_key, repo_id, run_dir, script_path, pid, process_started_at,
                 process_started_at, command_line, title.strip(),
                 str(resolved_log_path) if resolved_log_path is not None else None,
                 watch_json, timestamp),
            )
            _event(conn, key=run_key, timestamp=timestamp, before=None, state="run_started", detail=detail)
    except sqlite3.IntegrityError as exc:
        existing = _adopted_run_for_identity(conn, pid, process_started_at)
        if existing is not None:
            return {"run": dict(existing), "replayed": True}
        raise ValidationError("adopt_run_key_conflict", detail={"run_key": run_key}) from exc
    row = conn.execute("SELECT * FROM bg_runs WHERE run_key=?", (run_key,)).fetchone()
    return {"run": dict(row), "replayed": False}


def _scan_adopted(conn: sqlite3.Connection, timestamp: str, *, process_info=None,
                  progress_cache: dict[str, tuple[list[dict], int]] | None = None) -> dict:
    process_lookup = process_info or _process_info
    stall_after = _stall_minutes(conn) * 60
    rows = conn.execute(
        "SELECT * FROM bg_runs WHERE source='adopted' AND status='running' ORDER BY started_at,run_key"
    ).fetchall()
    event_count = 0
    watch_truncated = 0
    repo_cache = {}
    progress_cache = progress_cache if progress_cache is not None else {}
    for row in rows:
        try:
            process = process_lookup(row["pid"])
        except ProcessInfoUnavailable:
            # An unavailable probe is not proof that the process exited.
            continue
        if not _process_identity_matches(row["pid"], row["process_started_at"], process):
            with tx(conn):
                current = conn.execute(
                    "SELECT * FROM bg_runs WHERE run_key=? AND source='adopted' AND status='running'",
                    (row["run_key"],),
                ).fetchone()
                if current is None:
                    continue
                detail = {
                    "source": "adopted", "title": current["title"], "pid": current["pid"],
                    "process_started_at": current["process_started_at"],
                    "progress_hint": current["progress_hint"], "exit_code": None,
                }
                conn.execute(
                    "UPDATE bg_runs SET status='exited',finished_at=?,exit_code=NULL,orphaned=0,"
                    "last_notified_status='exited',updated_at=? WHERE run_key=?",
                    (timestamp, timestamp, current["run_key"]),
                )
                _event(conn, key=current["run_key"], timestamp=timestamp, before="running",
                       state="run_exited", detail=detail)
            event_count += 1
            continue

        if row["repo_id"] not in repo_cache:
            repo_cache[row["repo_id"]] = conn.execute(
                "SELECT path,run_watch_globs,run_progress_rules FROM repos WHERE repo_id=?",
                (row["repo_id"],)
            ).fetchone()
        repo = repo_cache[row["repo_id"]]
        if repo is None:
            continue
        patterns = list(_watch_globs(repo["run_watch_globs"]))
        for pattern in _watch_globs(row["watch_globs"]):
            if pattern not in patterns:
                patterns.append(pattern)
        watched_paths, truncated = _watch_paths(repo["path"], patterns)
        watch_truncated += truncated
        if row["repo_id"] not in progress_cache:
            progress_cache[row["repo_id"]] = _progress_rule_results(repo["path"], repo["run_progress_rules"])
        rule_results = progress_cache[row["repo_id"]][0]
        extra_paths = list(watched_paths)
        if row["log_path"]:
            extra_paths.insert(0, row["log_path"])
        log_hint, log_hint_mtime, log_mtime, _error_line = _latest_log_data(
            row["run_dir"], extra_paths=extra_paths
        )
        progress_hint = _best_progress_hint(log_hint, log_hint_mtime, rule_results)
        rule_mtime = _latest_timestamp(*(item.get("mtime") for item in rule_results
                                         if item.get("progress_hint") is not None))
        log_mtime = _latest_timestamp(log_mtime, rule_mtime)
        log_age = _age_seconds(log_mtime, timestamp) if log_mtime else None
        with tx(conn):
            current = conn.execute(
                "SELECT * FROM bg_runs WHERE run_key=? AND source='adopted' AND status='running'",
                (row["run_key"],),
            ).fetchone()
            if current is None:
                continue
            previous_hint = current["progress_hint"]
            previous_mtime = current["log_mtime"]
            if progress_hint is None:
                progress_hint = previous_hint
            mtime_changed = log_mtime is not None and log_mtime != previous_mtime
            progress_at = current["progress_at"]
            if progress_hint is not None and (progress_hint != previous_hint or mtime_changed):
                progress_at = timestamp
            stall_notified = 0 if mtime_changed else current["stall_notified"]
            conn.execute(
                "UPDATE bg_runs SET progress_hint=?,progress_at=?,log_mtime=?,stall_notified=?,"
                "orphaned=0,updated_at=? WHERE run_key=?",
                (progress_hint, progress_at, log_mtime or previous_mtime, stall_notified,
                 timestamp, current["run_key"]),
            )
            if log_age is not None and log_age > stall_after and not stall_notified:
                detail = {
                    "source": "adopted", "title": current["title"], "pid": current["pid"],
                    "progress_hint": progress_hint, "log_mtime": log_mtime,
                }
                _event(conn, key=current["run_key"], timestamp=timestamp, before="running",
                       state="run_stalled", detail=detail)
                conn.execute(
                    "UPDATE bg_runs SET stall_notified=1,last_notified_status='stalled' WHERE run_key=?",
                    (current["run_key"],),
                )
                event_count += 1
    return {"events": event_count, "watch_truncated": watch_truncated}


def scan_runs(conn: sqlite3.Connection, *, state_root: str | Path | None = None,
              now_ts: str | None = None, process_info=None) -> dict:
    """Read background-state records and logs, tracking only registered repositories."""
    timestamp = now_ts or now()
    records, seen, skipped = _load_records(_state_root(state_root))
    stall_after = _stall_minutes(conn) * 60
    tracked = 0
    event_count = 0
    watch_truncated = 0
    watch_cache: dict[str, tuple[str | None, int]] = {}
    repo_cache: dict[str, sqlite3.Row] = {}
    progress_cache: dict[str, tuple[list[dict], int]] = {}
    with tx(conn):
        for record in records:
            repo_id, _repo_path = _repo_for_working_directory(conn, record["working_directory"])
            if repo_id is None:
                continue
            tracked += 1
            log_hint, log_hint_mtime, log_mtime, error_line = _latest_log_data(record["run_dir"])
            if repo_id not in repo_cache:
                repo_cache[repo_id] = conn.execute(
                    "SELECT path,run_watch_globs,run_progress_rules FROM repos WHERE repo_id=?", (repo_id,)
                ).fetchone()
            repo = repo_cache[repo_id]
            if repo is None:
                continue
            if repo_id not in watch_cache:
                watch_cache[repo_id] = _watch_mtime(repo["path"], _watch_globs(repo["run_watch_globs"]))
                watch_truncated += watch_cache[repo_id][1]
            previous = conn.execute("SELECT * FROM bg_runs WHERE run_key=?", (record["run_key"],)).fetchone()
            previous_status = previous["status"] if previous else None
            previous_mtime = previous["log_mtime"] if previous else None
            previous_hint = previous["progress_hint"] if previous else None
            progress_at = previous["progress_at"] if previous else None
            frozen = previous is not None and record["status"] != "running"
            if frozen:
                progress_hint = previous_hint
                log_mtime = previous_mtime
            else:
                rule_results = []
                if record["status"] == "running":
                    if repo_id not in progress_cache:
                        progress_cache[repo_id] = _progress_rule_results(
                            repo["path"], repo["run_progress_rules"]
                        )
                    rule_results = progress_cache[repo_id][0]
                progress_hint = _best_progress_hint(log_hint, log_hint_mtime, rule_results)
                rule_mtime = _latest_timestamp(*(item.get("mtime") for item in rule_results
                                                 if item.get("progress_hint") is not None))
                log_mtime = _latest_timestamp(log_mtime, watch_cache[repo_id][0], rule_mtime)
                if progress_hint is None:
                    progress_hint = previous_hint
            log_age = _age_seconds(log_mtime, timestamp) if log_mtime else None
            first_seen_stale_running = (
                previous is None and record["status"] == "running"
                and (log_mtime is None or log_age is None or log_age > stall_after)
            )
            if not frozen and progress_hint is not None and (progress_hint != previous_hint or log_mtime != previous_mtime):
                progress_at = timestamp
            mtime_changed = log_mtime is not None and log_mtime != previous_mtime
            if first_seen_stale_running:
                stall_notified = 1
            else:
                stall_notified = 0 if mtime_changed else (previous["stall_notified"] if previous else 0)

            orphaned = (previous["orphaned"] if previous and record["status"] == "running" else 0)
            conn.execute(
                "INSERT INTO bg_runs(run_key,repo_id,run_dir,script_path,status,pid,started_at,finished_at,"
                "exit_code,progress_hint,progress_at,log_mtime,last_notified_status,stall_notified,orphaned,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_key) DO UPDATE SET "
                "repo_id=excluded.repo_id,run_dir=excluded.run_dir,script_path=excluded.script_path,status=excluded.status,"
                "pid=excluded.pid,started_at=excluded.started_at,finished_at=excluded.finished_at,"
                "exit_code=excluded.exit_code,progress_hint=excluded.progress_hint,progress_at=excluded.progress_at,"
                "log_mtime=excluded.log_mtime,last_notified_status=excluded.last_notified_status,"
                "stall_notified=excluded.stall_notified,orphaned=excluded.orphaned,updated_at=excluded.updated_at",
                (record["run_key"], repo_id, record["run_dir"], record["script_path"], record["status"],
                 record["pid"], record["started_at"], record["finished_at"], record["exit_code"],
                 progress_hint, progress_at, log_mtime,
                 record["status"] if previous is None else previous["last_notified_status"],
                 stall_notified, orphaned, timestamp),
            )

            event_state = None
            event_before = previous_status
            if previous is None and record["status"] == "running" and not first_seen_stale_running:
                event_state = "run_started"
                event_before = None
            elif previous is not None and previous_status != record["status"]:
                event_state = _EVENT_FOR_STATUS.get(record["status"])
            event_detail = {
                "run_key": record["run_key"], "repo_id": repo_id, "run_dir": record["run_dir"],
                "script_path": record["script_path"], "status": record["status"],
                "progress_hint": progress_hint, "exit_code": record["exit_code"],
                "error_line": error_line,
            }
            if event_state:
                _event(conn, key=record["run_key"], timestamp=timestamp, before=event_before,
                       state=event_state, detail=event_detail)
                event_count += 1
                conn.execute("UPDATE bg_runs SET last_notified_status=? WHERE run_key=?",
                             (record["status"], record["run_key"]))

            current_run = conn.execute("SELECT * FROM bg_runs WHERE run_key=?", (record["run_key"],)).fetchone()
            age = log_age
            if (record["status"] == "running" and age is not None and age > stall_after
                    and not current_run["stall_notified"]):
                _event(conn, key=record["run_key"], timestamp=timestamp, before="running",
                       state="run_stalled", detail=event_detail)
                conn.execute("UPDATE bg_runs SET stall_notified=1,last_notified_status='stalled' WHERE run_key=?",
                             (record["run_key"],))
                event_count += 1
    adopted = _scan_adopted(conn, timestamp, process_info=process_info, progress_cache=progress_cache)
    progress_rule_errors = sum(errors for _results, errors in progress_cache.values())
    return {"seen": seen, "tracked": tracked, "events": event_count + adopted["events"],
            "skipped": skipped, "watch_truncated": watch_truncated + adopted["watch_truncated"],
            "progress_rule_errors": progress_rule_errors}


def list_runs(conn: sqlite3.Connection, repo_id: str | None = None) -> list[dict]:
    if repo_id is None:
        rows = conn.execute("SELECT * FROM bg_runs ORDER BY started_at DESC,run_key").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM bg_runs WHERE repo_id=? ORDER BY started_at DESC,run_key", (repo_id,)
        ).fetchall()
    return [dict(row) for row in rows]
