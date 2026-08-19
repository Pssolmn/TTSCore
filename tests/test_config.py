from pathlib import Path

import pytest

from readji_tts.config import guard_remote_database, resolve_env_file


def test_resolve_env_file_uses_only_this_workers_default_env(monkeypatch) -> None:
    monkeypatch.delenv("TTS_ENV_FILE", raising=False)

    assert resolve_env_file() == Path(".env")


def test_resolve_env_file_honours_an_explicit_worker_env(monkeypatch) -> None:
    monkeypatch.setenv("TTS_ENV_FILE", "C:/customer-worker/.env.production")

    assert resolve_env_file() == Path("C:/customer-worker/.env.production")


def test_remote_database_guard_accepts_explicit_settings_confirmation(monkeypatch) -> None:
    monkeypatch.delenv("TTS_CONFIRM_REMOTE", raising=False)

    guard_remote_database("postgresql://postgres:password@remote.example/railway", confirmed=True)


def test_remote_database_guard_rejects_without_any_confirmation(monkeypatch) -> None:
    monkeypatch.delenv("TTS_CONFIRM_REMOTE", raising=False)

    with pytest.raises(RuntimeError, match="Refusing to start"):
        guard_remote_database("postgresql://postgres:password@remote.example/railway")
