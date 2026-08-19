from pathlib import Path
from unittest.mock import MagicMock

from botocore.exceptions import ClientError
import pytest

from readji_tts.r2 import R2Storage
from readji_tts.r2_upload_quota import (
    BYTES_PER_GIB,
    DEFAULT_LIMIT_BYTES,
    R2UploadQuotaExceeded,
    R2UploadQuotaStore,
)


def test_new_record_starts_at_zero_with_the_six_gib_guard_enabled(tmp_path: Path) -> None:
    status = R2UploadQuotaStore(tmp_path / "runtime" / "quota.json").status()

    assert status.enabled is True
    assert status.limit_bytes == DEFAULT_LIMIT_BYTES == 6 * BYTES_PER_GIB
    assert status.used_bytes == 0
    assert status.pending_bytes == 0
    assert status.started_at


def test_completed_upload_is_persisted_and_counts_from_the_new_record_only(tmp_path: Path) -> None:
    path = tmp_path / "quota.json"
    store = R2UploadQuotaStore(path)
    reservation = store.reserve(key="episode-audio/1/full.mp3", byte_size=123)
    store.confirm(reservation)

    reloaded = R2UploadQuotaStore(path).status()
    assert reloaded.used_bytes == 123
    assert reloaded.pending_bytes == 0
    assert reloaded.remaining_bytes == DEFAULT_LIMIT_BYTES - 123


def test_pending_reservation_blocks_an_over_limit_upload_until_released(tmp_path: Path) -> None:
    store = R2UploadQuotaStore(tmp_path / "quota.json")
    store.configure(enabled=True, limit_bytes=10)
    reservation = store.reserve(key="episode-audio/1/full.mp3", byte_size=8)

    with pytest.raises(R2UploadQuotaExceeded, match="quota exceeded"):
        store.reserve(key="episode-audio/2/full.mp3", byte_size=3)

    store.release(reservation)
    assert store.reserve(key="episode-audio/2/full.mp3", byte_size=3) is not None


def test_disabling_quota_stops_new_reservations_without_erasing_existing_record(tmp_path: Path) -> None:
    store = R2UploadQuotaStore(tmp_path / "quota.json")
    reservation = store.reserve(key="episode-audio/1/full.mp3", byte_size=7)
    store.confirm(reservation)
    disabled = store.configure(enabled=False, limit_bytes=10)

    assert disabled.used_bytes == 7
    assert store.reserve(key="episode-audio/2/full.mp3", byte_size=999_999) is None
    resumed = store.configure(enabled=True, limit_bytes=10)
    assert resumed.used_bytes == 7


def test_invalid_record_fails_closed_instead_of_resetting_usage(tmp_path: Path) -> None:
    path = tmp_path / "quota.json"
    path.write_text('{"version": 1, "enabled": true}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid"):
        R2UploadQuotaStore(path).status()


def test_failed_r2_put_releases_only_when_head_confirms_the_object_is_missing(tmp_path: Path) -> None:
    local_mp3 = tmp_path / "full.mp3"
    local_mp3.write_bytes(b"audio")
    storage = object.__new__(R2Storage)
    storage.bucket = "test-bucket"
    storage.upload_quota = R2UploadQuotaStore(tmp_path / "quota.json")
    storage.upload_quota.configure(enabled=True, limit_bytes=10)
    storage.client = MagicMock()
    storage.client.put_object.side_effect = RuntimeError("network timeout")
    storage.client.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")

    with pytest.raises(RuntimeError, match="network timeout"):
        storage.upload_mp3("episode-audio/1/full.mp3", local_mp3)

    assert storage.upload_quota.status().used_bytes == 0


def test_failed_r2_put_keeps_reservation_when_object_presence_cannot_be_confirmed(tmp_path: Path) -> None:
    local_mp3 = tmp_path / "full.mp3"
    local_mp3.write_bytes(b"audio")
    storage = object.__new__(R2Storage)
    storage.bucket = "test-bucket"
    storage.upload_quota = R2UploadQuotaStore(tmp_path / "quota.json")
    storage.upload_quota.configure(enabled=True, limit_bytes=10)
    storage.client = MagicMock()
    storage.client.put_object.side_effect = RuntimeError("network timeout")
    storage.client.head_object.side_effect = RuntimeError("R2 unavailable")

    with pytest.raises(RuntimeError, match="network timeout"):
        storage.upload_mp3("episode-audio/1/full.mp3", local_mp3)

    status = storage.upload_quota.status()
    assert status.used_bytes == 5
    assert status.pending_bytes == 5
