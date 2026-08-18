from pathlib import Path

from readji_tts.config import resolve_env_file


def test_resolve_env_file_uses_only_this_workers_default_env(monkeypatch) -> None:
    monkeypatch.delenv("TTS_ENV_FILE", raising=False)

    assert resolve_env_file() == Path(".env")


def test_resolve_env_file_honours_an_explicit_worker_env(monkeypatch) -> None:
    monkeypatch.setenv("TTS_ENV_FILE", "C:/customer-worker/.env.production")

    assert resolve_env_file() == Path("C:/customer-worker/.env.production")
