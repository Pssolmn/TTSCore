"""A local, forward-only storage quota for new TTS audio uploaded to R2.

The quota intentionally does not inspect or count pre-existing bucket objects.
Its record starts when this state file is first created, so it is safe to turn
on for an already-populated development bucket.  It is a guard for the one
manual TTSCore installation that owns this state file; two machines writing to
the same bucket must share this state file on a shared volume or each be given
a smaller separate allowance.
"""
from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterator
from uuid import uuid4


BYTES_PER_GIB = 1024**3
DEFAULT_LIMIT_BYTES = 6 * BYTES_PER_GIB
_VERSION = 1
_RECENT_UPLOAD_LIMIT = 100
_LOCK_TIMEOUT_SECONDS = 5.0


class R2UploadQuotaError(RuntimeError):
    """The local quota record cannot safely be used."""


class R2UploadQuotaExceeded(R2UploadQuotaError):
    """A new object would exceed the configured retained-audio allowance."""


@dataclass(frozen=True)
class R2UploadQuotaStatus:
    enabled: bool
    limit_bytes: int
    used_bytes: int
    pending_bytes: int
    started_at: str

    @property
    def remaining_bytes(self) -> int:
        return max(self.limit_bytes - self.used_bytes, 0)


@dataclass(frozen=True)
class R2UploadReservation:
    token: str
    key: str
    byte_size: int


class R2UploadQuotaStore:
    """Durably reserve new R2 MP3 bytes before uploading them.

    ``used_bytes`` includes both completed uploads and pending reservations.
    That is deliberately conservative: an abrupt shutdown after R2 accepted
    an object cannot make the next process accidentally exceed the cap.  A
    normal failed upload or a successfully deleted stale upload releases its
    reservation again.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self._thread_lock = threading.RLock()

    def status(self) -> R2UploadQuotaStatus:
        with self._locked():
            state = self._load_locked()
            return _status_from_state(state)

    def configure(self, *, enabled: bool, limit_bytes: int) -> R2UploadQuotaStatus:
        if not isinstance(enabled, bool):
            raise R2UploadQuotaError("R2 upload quota enabled must be true or false")
        if isinstance(limit_bytes, bool) or not isinstance(limit_bytes, int) or limit_bytes < 1:
            raise R2UploadQuotaError("R2 upload quota limit must be a positive whole number of bytes")
        with self._locked():
            state = self._load_locked()
            state["enabled"] = enabled
            state["limit_bytes"] = limit_bytes
            self._write_locked(state)
            return _status_from_state(state)

    def reserve(self, *, key: str, byte_size: int) -> R2UploadReservation | None:
        if not key:
            raise R2UploadQuotaError("R2 upload quota requires a non-empty object key")
        if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 1:
            raise R2UploadQuotaError("R2 upload quota requires a positive whole file size")

        with self._locked():
            state = self._load_locked()
            if not state["enabled"]:
                return None
            remaining = int(state["limit_bytes"]) - int(state["used_bytes"])
            if byte_size > remaining:
                raise R2UploadQuotaExceeded(
                    "R2 upload quota exceeded: "
                    f"requested {byte_size} bytes, only {max(remaining, 0)} bytes remain "
                    f"out of {state['limit_bytes']} bytes."
                )

            token = uuid4().hex
            state["used_bytes"] += byte_size
            state["reservations"][token] = {
                "key": key,
                "byte_size": byte_size,
                "reserved_at": _now(),
            }
            self._write_locked(state)
            return R2UploadReservation(token=token, key=key, byte_size=byte_size)

    def confirm(self, reservation: R2UploadReservation | None) -> None:
        """Mark a successfully retained object as completed without changing usage."""
        if reservation is None:
            return
        with self._locked():
            state = self._load_locked()
            saved = state["reservations"].pop(reservation.token, None)
            if saved is None:
                return
            recent = state["recent_uploads"]
            recent.append(
                {
                    "key": saved["key"],
                    "byte_size": saved["byte_size"],
                    "uploaded_at": _now(),
                }
            )
            del recent[:-_RECENT_UPLOAD_LIMIT]
            self._write_locked(state)

    def release(self, reservation: R2UploadReservation | None) -> None:
        """Release bytes only after the corresponding R2 object is absent."""
        if reservation is None:
            return
        with self._locked():
            state = self._load_locked()
            saved = state["reservations"].pop(reservation.token, None)
            if saved is None:
                return
            state["used_bytes"] = max(0, int(state["used_bytes"]) - int(saved["byte_size"]))
            self._write_locked(state)

    def _load_locked(self) -> dict[str, Any]:
        if not self.path.exists():
            state = _new_state()
            self._write_locked(state)
            return state
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise R2UploadQuotaError(f"Unable to read R2 upload quota record {self.path}: {error}") from error
        return _validated_state(raw, source=str(self.path))

    def _write_locked(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        except OSError as error:
            with suppress(OSError):
                temporary.unlink()
            raise R2UploadQuotaError(f"Unable to save R2 upload quota record {self.path}: {error}") from error

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            with self.lock_path.open("a+b") as lock_file:
                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"0")
                    lock_file.flush()
                _acquire_file_lock(lock_file)
                try:
                    yield
                finally:
                    _release_file_lock(lock_file)


def _new_state() -> dict[str, Any]:
    return {
        "version": _VERSION,
        # The worker is deliberately conservative for this emergency guard:
        # first start after installing the feature turns on a fresh 6 GiB cap.
        "enabled": True,
        "limit_bytes": DEFAULT_LIMIT_BYTES,
        "used_bytes": 0,
        "started_at": _now(),
        "reservations": {},
        "recent_uploads": [],
    }


def _validated_state(raw: Any, *, source: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("version") != _VERSION:
        raise R2UploadQuotaError(f"R2 upload quota record {source} must contain version={_VERSION}")
    if not isinstance(raw.get("enabled"), bool):
        raise R2UploadQuotaError(f"R2 upload quota record {source} has an invalid enabled value")
    if not isinstance(raw.get("started_at"), str) or not raw["started_at"]:
        raise R2UploadQuotaError(f"R2 upload quota record {source} has an invalid started_at value")
    for field in ("limit_bytes", "used_bytes"):
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise R2UploadQuotaError(f"R2 upload quota record {source} has an invalid {field} value")
    if raw["limit_bytes"] < 1:
        raise R2UploadQuotaError(f"R2 upload quota record {source} must have a positive limit_bytes value")
    if not isinstance(raw.get("reservations"), dict) or not isinstance(raw.get("recent_uploads"), list):
        raise R2UploadQuotaError(f"R2 upload quota record {source} has an invalid reservations or recent_uploads value")

    reservations: dict[str, dict[str, Any]] = {}
    pending_bytes = 0
    for token, entry in raw["reservations"].items():
        if not isinstance(token, str) or not isinstance(entry, dict):
            raise R2UploadQuotaError(f"R2 upload quota record {source} has an invalid reservation")
        key = entry.get("key")
        byte_size = entry.get("byte_size")
        reserved_at = entry.get("reserved_at")
        if not isinstance(key, str) or not key or isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 1 or not isinstance(reserved_at, str):
            raise R2UploadQuotaError(f"R2 upload quota record {source} has an invalid reservation entry")
        reservations[token] = {"key": key, "byte_size": byte_size, "reserved_at": reserved_at}
        pending_bytes += byte_size

    recent_uploads: list[dict[str, Any]] = []
    for entry in raw["recent_uploads"][-_RECENT_UPLOAD_LIMIT:]:
        if not isinstance(entry, dict):
            raise R2UploadQuotaError(f"R2 upload quota record {source} has an invalid recent upload")
        key = entry.get("key")
        byte_size = entry.get("byte_size")
        uploaded_at = entry.get("uploaded_at")
        if not isinstance(key, str) or not key or isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 1 or not isinstance(uploaded_at, str):
            raise R2UploadQuotaError(f"R2 upload quota record {source} has an invalid recent upload entry")
        recent_uploads.append({"key": key, "byte_size": byte_size, "uploaded_at": uploaded_at})

    if raw["used_bytes"] < pending_bytes:
        raise R2UploadQuotaError(f"R2 upload quota record {source} has used_bytes below pending reservations")
    return {
        "version": _VERSION,
        "enabled": raw["enabled"],
        "limit_bytes": raw["limit_bytes"],
        "used_bytes": raw["used_bytes"],
        "started_at": raw["started_at"],
        "reservations": reservations,
        "recent_uploads": recent_uploads,
    }


def _status_from_state(state: dict[str, Any]) -> R2UploadQuotaStatus:
    pending_bytes = sum(int(entry["byte_size"]) for entry in state["reservations"].values())
    return R2UploadQuotaStatus(
        enabled=bool(state["enabled"]),
        limit_bytes=int(state["limit_bytes"]),
        used_bytes=int(state["used_bytes"]),
        pending_bytes=pending_bytes,
        started_at=str(state["started_at"]),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _acquire_file_lock(lock_file: Any) -> None:
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    if os.name == "nt":
        import msvcrt

        while True:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise R2UploadQuotaError("Timed out waiting for the R2 upload quota record") from error
                time.sleep(0.05)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _release_file_lock(lock_file: Any) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
