import os
from datetime import datetime, timezone
from pathlib import Path

from .errors import ValidationError


def home() -> Path:
    raw = os.environ.get("LABOPS_HOME")
    if raw is None:
        raw = str(Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".labops")
    if raw.startswith(("\\\\", "//")):
        raise ValidationError("unc_not_allowed", detail={"path": raw})
    return Path(raw).expanduser().resolve()


def _under_temp(path: Path) -> bool:
    # Opus review fix: test-instance bypasses (non-TTY approval, crashpoints, fake clock) are only
    # honoured for a LABOPS_HOME inside the system temp dir, so `init --test-instance` cannot turn a
    # real home into one. Anti-mistake only, same boundary as approvals.require_human.
    import tempfile
    temp = Path(tempfile.gettempdir()).resolve()
    try:
        path.resolve().relative_to(temp)
    except ValueError:
        return False
    return True


def is_test_instance() -> bool:
    root = home()
    return (root / ".test-instance").is_file() and _under_temp(root)


def now() -> str:
    fake = os.environ.get("LABOPS_FAKE_NOW")
    if fake and is_test_instance():
        try:
            parsed = datetime.fromisoformat(fake.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("invalid_fake_now", detail={"value": fake}) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_layout() -> Path:
    root = home()
    for name in ("locks", "logs", "handoffs", "verify", "worktrees"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def initialize(*, test_instance: bool = False) -> Path:
    if test_instance and not _under_temp(home()):
        raise ValidationError("test_instance_requires_temp_home", detail={"home": str(home())})
    root = ensure_layout()
    if test_instance:
        (root / ".test-instance").touch(exist_ok=True)
    return root
