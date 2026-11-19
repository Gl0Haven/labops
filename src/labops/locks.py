import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import Blocked

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def _open_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    return handle


def _lock(handle) -> bool:
    try:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, BlockingIOError):
        return False


def _acquire(handle, *, blocking: bool, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        if _lock(handle):
            return True
        if not blocking or time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _unlock(handle) -> None:
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path, *, blocking: bool = True, timeout_s: float = 30.0) -> Iterator[None]:
    handle = _open_lock(Path(path))
    acquired = False
    try:
        acquired = _acquire(handle, blocking=blocking, timeout_s=timeout_s)
        if not acquired:
            raise Blocked("lock_unavailable", detail={"path": str(path), "blocking": blocking, "timeout_s": timeout_s})
        yield
    finally:
        if acquired:
            _unlock(handle)
        handle.close()


@contextmanager
def try_lock(path: Path):
    handle = _open_lock(Path(path))
    acquired = False
    try:
        acquired = _acquire(handle, blocking=False, timeout_s=0)
        yield acquired
    finally:
        if acquired:
            _unlock(handle)
        handle.close()
