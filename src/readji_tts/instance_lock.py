from __future__ import annotations

import msvcrt
from pathlib import Path
from typing import IO


class AlreadyRunningError(RuntimeError):
    """Raised when another process already holds the worker's instance lock."""


class InstanceLock:
    def __init__(self, handle: IO[bytes]) -> None:
        self._handle: IO[bytes] | None = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._handle.close()
            self._handle = None


def acquire_single_instance_lock(lock_path: Path) -> InstanceLock:
    """Acquire an exclusive, self-releasing lock so only one worker (GUI or
    headless) can run on this machine's GPU at a time.

    Uses msvcrt.locking() rather than a PID file: the OS releases this lock
    the instant the holding process exits for any reason -- clean shutdown,
    crash, or a forced kill -- with no stale-PID bookkeeping required.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        # msvcrt.locking() locks a byte range; the file needs at least 1 byte
        # to lock. A leftover byte from a prior clean run is fine to reuse.
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as error:
        handle.close()
        raise AlreadyRunningError(
            "Another copy of the Readji TTS worker is already using this machine's GPU."
        ) from error
    return InstanceLock(handle)
