from pathlib import Path
from types import SimpleNamespace

import pytest

from readji_tts import worker as worker_module


class _FakeLock:
    def __init__(self) -> None:
        self.release_count = 0

    def release(self) -> None:
        self.release_count += 1


class _FakeRepository:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        database_url="postgresql://postgres:password@localhost/readji",
        remote_database_confirmed=False,
        work_dir=tmp_path,
        worker_id="test-worker",
        lease_seconds=300,
        voice_basic_path=tmp_path / "Basic",
        voice_variants_path=tmp_path,
    )


def test_worker_releases_repository_and_lock_when_initialization_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _FakeLock()
    repository = _FakeRepository()
    monkeypatch.setattr(worker_module, "acquire_single_instance_lock", lambda _path: lock)
    monkeypatch.setattr(worker_module, "JobRepository", lambda *_args, **_kwargs: repository)
    monkeypatch.setattr(worker_module, "R2Storage", lambda _settings: (_ for _ in ()).throw(RuntimeError("R2 failed")))

    with pytest.raises(RuntimeError, match="R2 failed"):
        worker_module.Worker(_settings(tmp_path))  # type: ignore[arg-type]

    assert repository.close_count == 1
    assert lock.release_count == 1


def test_worker_close_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = _FakeLock()
    repository = _FakeRepository()
    monkeypatch.setattr(worker_module, "acquire_single_instance_lock", lambda _path: lock)
    monkeypatch.setattr(worker_module, "JobRepository", lambda *_args, **_kwargs: repository)
    monkeypatch.setattr(worker_module, "R2Storage", lambda _settings: object())
    monkeypatch.setattr(worker_module, "load_basic_voice_profiles", lambda _path: {})

    worker = worker_module.Worker(_settings(tmp_path))  # type: ignore[arg-type]
    worker.close()
    worker.close()

    assert repository.close_count == 1
    assert lock.release_count == 1
