import os
import sqlite3
from pathlib import Path, PureWindowsPath

from . import gitio
from .backup import run_backup, verify_restore
from .errors import Blocked, LabOpsError, ValidationError
from .index import rebuild_index
from .legacy import audit_legacy
from .tasks import add_repo, repo_row, set_confidential, set_repo_options


def _resolve_main_ref(path: Path, value: str) -> str:
    if value != "auto":
        return value
    result = gitio.run(path, "symbolic-ref", "--quiet", "HEAD", check=False)
    if result.returncode:
        raise ValidationError("onboard_main_ref_auto_failed", detail={
            "returncode": result.returncode,
            "stderr": result.stderr.decode("utf-8", "surrogateescape").strip(),
        })
    ref = result.stdout.decode("utf-8", "surrogateescape").strip()
    if not ref.startswith("refs/heads/") or gitio.read_ref(path, ref) is None:
        raise ValidationError("onboard_main_ref_auto_invalid", detail={"ref": ref})
    return ref


def _step(name: str, completed: list[str], operation):
    try:
        result = operation()
    except LabOpsError as exc:
        exc.detail = {**(exc.detail or {}), "onboard_step": name, "completed_steps": list(completed)}
        raise
    completed.append(name)
    return result


def onboard(conn: sqlite3.Connection, *, repo_id: str, path: Path, main_ref: str = "auto",
            confidential: bool = False, legacy_bridge: str | None = None,
            backup_root: str | None = None, backup_keep: int | None = None,
            index: bool = False) -> dict:
    repo_path = Path(path).expanduser().resolve()
    completed = []
    steps = {}
    resolved_ref_holder = {}

    def register():
        if not repo_path.is_dir():
            raise ValidationError("repo_not_git", detail={"path": str(repo_path)})
        resolved_ref = _resolve_main_ref(repo_path, main_ref)
        resolved_ref_holder["value"] = resolved_ref
        existing = conn.execute("SELECT * FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
        if existing is not None:
            if os.path.normcase(str(Path(existing["path"]).resolve())) != os.path.normcase(str(repo_path)):
                raise ValidationError("repo_id_path_conflict", detail={"repo_id": repo_id,
                                                                         "registered_path": existing["path"],
                                                                         "requested_path": str(repo_path)})
            if existing["main_ref"] != resolved_ref:
                raise ValidationError("repo_main_ref_conflict", detail={"repo_id": repo_id,
                                                                         "registered": existing["main_ref"],
                                                                         "requested": resolved_ref})
            return {"registered": False, "repo_id": repo_id}
        result = add_repo(conn, repo_id=repo_id, path=repo_path, main_ref=resolved_ref,
                          trusted_for_exec=False, confidential=confidential)
        return {"registered": True, **result}

    steps["register"] = _step("register", completed, register)
    resolved_ref = resolved_ref_holder["value"]
    repo = repo_row(conn, repo_id)

    if confidential and not repo["confidential"]:
        steps["confidential"] = _step("confidential", completed,
                                      lambda: set_confidential(conn, repo_id, True))
    else:
        completed.append("confidential")
        steps["confidential"] = {"confidential": bool(repo["confidential"]), "changed": False}

    backup_config_changed = {"value": False}

    def configure_settings():
        requested_options = {}
        if legacy_bridge is not None:
            relative_bridge = Path(legacy_bridge)
            windows_bridge = PureWindowsPath(legacy_bridge)
            if relative_bridge.is_absolute() or windows_bridge.is_absolute() or windows_bridge.drive:
                raise ValidationError("legacy_bridge_must_be_relative", detail={"path": legacy_bridge})
            if repo["legacy_bridge"] != legacy_bridge:
                requested_options["legacy_bridge"] = legacy_bridge
        if backup_root is not None:
            candidate_backup_root = Path(backup_root).expanduser()
            if not candidate_backup_root.is_absolute():
                raise ValidationError("backup_root_must_be_absolute", detail={"path": backup_root})
            resolved_backup_root = str(candidate_backup_root.resolve())
            if repo["backup_root"] != resolved_backup_root:
                requested_options["backup_root"] = backup_root
                backup_config_changed["value"] = True
            target_keep = backup_keep if backup_keep is not None else 7
            if repo["backup_keep"] != target_keep:
                requested_options["backup_keep"] = target_keep
                backup_config_changed["value"] = True
        elif backup_keep is not None and repo["backup_keep"] != backup_keep:
            requested_options["backup_keep"] = backup_keep
            backup_config_changed["value"] = True
        if requested_options:
            return set_repo_options(conn, repo_id, **requested_options)
        return {"changed": False}

    steps["settings"] = _step("settings", completed, configure_settings)
    repo = repo_row(conn, repo_id)

    if repo["legacy_bridge"]:
        steps["legacy"] = _step("legacy", completed, lambda: audit_legacy(conn, repo_id))
    else:
        completed.append("legacy")
        steps["legacy"] = {"skipped": True, "reason": "legacy_bridge_not_configured"}

    if repo["backup_root"]:
        def backup_step():
            latest = conn.execute(
                "SELECT backup_id,head,verified FROM backups WHERE repo_id=? "
                "ORDER BY created_at DESC,rowid DESC LIMIT 1", (repo_id,)
            ).fetchone()
            head = gitio.rev_parse(repo_path, "HEAD")
            if not backup_config_changed["value"] and latest is not None and latest["verified"] and latest["head"] == head:
                restored = verify_restore(conn, repo_id, backup_id=latest["backup_id"])
                if restored["ok"]:
                    return {"skipped": True, "backup_id": latest["backup_id"], "verify_restore": restored}
            backup = run_backup(conn, repo_id)
            restored = verify_restore(conn, repo_id, backup_id=backup["backup_id"])
            if not restored["ok"]:
                raise Blocked("onboard_backup_restore_failed", detail=restored)
            return {"backup": backup, "verify_restore": restored}

        steps["backup"] = _step("backup", completed, backup_step)
    else:
        completed.append("backup")
        steps["backup"] = {"skipped": True, "reason": "backup_root_not_configured"}

    if index:
        steps["index"] = _step("index", completed, lambda: rebuild_index(conn, repo_id))
    else:
        completed.append("index")
        steps["index"] = {"skipped": True, "reason": "index_not_requested"}

    return {"repo_id": repo_id, "path": str(repo_path), "main_ref": resolved_ref,
            "trusted_for_exec": bool(repo["trusted_for_exec"]), "steps": steps,
            "completed_steps": completed}
