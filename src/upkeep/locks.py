"""File locks, so separate `up` processes never run the same tool, or tools
sharing a lock, at the same time."""

from __future__ import annotations

import fcntl
import os
import re
import time
from collections.abc import Callable
from pathlib import Path


def _file(lock_dir: Path, name: str) -> Path:
    return lock_dir / (re.sub(r"[^A-Za-z0-9._-]", "_", name) + ".lock")


class FileLock:
    def __init__(self, lock_dir: Path, name: str):
        self.name = name
        self.path = _file(lock_dir, name)
        self.fd: int | None = None

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def acquire(
        self,
        on_wait: Callable[[str], None] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
        poll: float = 0.2,
    ) -> bool:
        """Block until held; False if `cancelled()` turned true first."""
        waited = False
        while not self.try_acquire():
            if cancelled():
                return False
            if not waited and on_wait:
                on_wait(self.name)
            waited = True
            time.sleep(poll)
        return True

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> FileLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def is_held(lock_dir: Path, name: str) -> bool:
    lock = FileLock(lock_dir, name)
    if not lock.path.exists():
        return False
    if lock.try_acquire():
        lock.release()
        return False
    return True
