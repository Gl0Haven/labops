class LabOpsError(Exception):
    code = "internal"
    exit = 10

    def __init__(self, message: str = "internal error", *, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


class UsageError(LabOpsError):
    code = "usage"
    exit = 2


class ValidationError(LabOpsError):
    code = "validation"
    exit = 3


class IdempotencyConflict(LabOpsError):
    code = "idempotency_conflict"
    exit = 4


class VersionConflict(LabOpsError):
    code = "version_conflict"
    exit = 4


class IllegalTransition(LabOpsError):
    code = "illegal_transition"
    exit = 4


class GitConflict(LabOpsError):
    code = "git_conflict"
    exit = 4


class Blocked(LabOpsError):
    code = "blocked"
    exit = 5
