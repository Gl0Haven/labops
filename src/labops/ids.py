import re
import secrets
from datetime import datetime, timezone

from .errors import ValidationError

TASK_ID_RE = re.compile(r"^T\d{8}-[a-z0-9][a-z0-9-]{0,39}$")
REPO_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
ACTORS = {"codex", "opus", "user"}
ROLES = {"implement", "fix"}
ID_PREFIXES = {"op", "cl", "ev", "rv", "ap", "ig", "bk"}


def new_id(prefix: str) -> str:
    if prefix not in ID_PREFIXES:
        raise ValidationError("invalid_id_prefix", detail={"prefix": prefix})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}_{stamp}_{secrets.token_hex(4)}"


def round_id(task_id: str, n: int) -> str:
    if not TASK_ID_RE.fullmatch(task_id) or n < 1:
        raise ValidationError("invalid_round_id", detail={"task_id": task_id, "n": n})
    return f"{task_id}/r{n}"


def validate_task_id(task_id: str) -> None:
    if not TASK_ID_RE.fullmatch(task_id):
        raise ValidationError("invalid_task_id", detail={"task_id": task_id})


def validate_repo_id(repo_id: str) -> None:
    if not REPO_ID_RE.fullmatch(repo_id):
        raise ValidationError("invalid_repo_id", detail={"repo_id": repo_id})


def validate_actor(actor: str) -> None:
    if actor not in ACTORS:
        raise ValidationError("invalid_actor", detail={"actor": actor})


def validate_role(role: str) -> None:
    if role not in ROLES:
        raise ValidationError("invalid_role", detail={"role": role})
