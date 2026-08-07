from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import os
import shutil
import socket

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


VOICE_SLOT_IDS = ("old_male", "young_male", "female")


@dataclass(frozen=True)
class VoiceProfile:
    slot: str
    label: str
    version: str
    reference_wav_path: Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(alias="DATABASE_URL")
    r2_endpoint_url: str = Field(alias="R2_ENDPOINT_URL")
    r2_access_key_id: str = Field(alias="R2_ACCESS_KEY_ID")
    r2_secret_access_key: str = Field(alias="R2_SECRET_ACCESS_KEY")
    r2_bucket_name: str = Field(alias="R2_BUCKET_NAME")
    r2_public_url: str = Field(alias="R2_PUBLIC_URL")

    master_voice_path: Path = Field(default=Path("assets/master-voice/readji-narrator.wav"), alias="TTS_MASTER_VOICE_PATH")
    voice_slots_path: Path = Field(default=Path("config/voice-slots.json"), alias="TTS_VOICE_SLOTS_PATH")
    model_id: str = Field(default="openbmb/VoxCPM2", alias="TTS_MODEL_ID")
    device: str = Field(default="cuda", alias="TTS_DEVICE")
    load_denoiser: bool = Field(default=False, alias="TTS_LOAD_DENOISER")
    optimize: bool = Field(default=False, alias="TTS_OPTIMIZE")
    inference_timesteps: int = Field(default=10, ge=1, le=50, alias="TTS_INFERENCE_TIMESTEPS")
    cfg_value: float = Field(default=2.0, ge=0.0, le=10.0, alias="TTS_CFG_VALUE")
    badcase_max_attempts: int = Field(default=2, ge=1, le=3, alias="TTS_BADCASE_MAX_ATTEMPTS")
    max_chunk_chars: int = Field(default=240, ge=80, le=500, alias="TTS_MAX_CHUNK_CHARS")
    # This is a hard safety ceiling.  The environment may lower it for a
    # maintenance window, but never raise it above the product limit.
    max_output_blocks: int = Field(default=500, ge=1, le=500, alias="TTS_MAX_OUTPUT_BLOCKS")
    poll_seconds: float = Field(default=5.0, ge=1.0, le=60.0, alias="TTS_POLL_SECONDS")
    lease_seconds: int = Field(default=1800, ge=300, le=7200, alias="TTS_LEASE_SECONDS")
    worker_id: str = Field(default_factory=lambda: f"readji-tts-{socket.gethostname()}", alias="TTS_WORKER_ID")
    work_dir: Path = Field(default=Path("runtime"), alias="TTS_WORK_DIR")
    ffmpeg_path: str = Field(default="ffmpeg", alias="TTS_FFMPEG_PATH")

    @field_validator("device")
    @classmethod
    def cuda_only(cls, value: str) -> str:
        if value != "cuda":
            raise ValueError("TTS_DEVICE must be cuda; CPU rendering is intentionally unsupported")
        return value

    @field_validator("r2_public_url")
    @classmethod
    def trim_public_url(cls, value: str) -> str:
        return value.rstrip("/")


def load_settings(*, require_master_voice: bool = False) -> Settings:
    configured_env_file = os.environ.get("TTS_ENV_FILE")
    local_env_file = Path(configured_env_file) if configured_env_file else Path(".env")
    # During the current Phase A setup the worker shares a private machine with
    # apps/api. Reusing its environment avoids duplicating database/R2 secrets.
    # A dedicated TTS_ENV_FILE or local .env always takes precedence for deploys.
    if not local_env_file.is_file() and not configured_env_file:
        local_env_file = Path(__file__).resolve().parents[3] / "Novel Platform" / "apps" / "api" / ".env"
    settings = Settings(_env_file=local_env_file)
    configured_ffmpeg = Path(settings.ffmpeg_path)
    candidates = [
        configured_ffmpeg if configured_ffmpeg.is_file() else None,
        Path(shutil.which(settings.ffmpeg_path)) if shutil.which(settings.ffmpeg_path) else None,
        # Current local setup fallback; deployments must use PATH or TTS_FFMPEG_PATH.
        Path("C:/ytdl/ffmpeg.exe") if os.name == "nt" else None,
    ]
    resolved_ffmpeg = next((candidate for candidate in candidates if candidate and candidate.is_file()), None)
    if resolved_ffmpeg is None:
        raise RuntimeError("ffmpeg was not found. Set TTS_FFMPEG_PATH to the ffmpeg executable.")
    settings.ffmpeg_path = str(resolved_ffmpeg)
    if require_master_voice and not settings.master_voice_path.is_file():
        raise RuntimeError(
            f"TTS_MASTER_VOICE_PATH is missing: {settings.master_voice_path}. "
            "Run readji-tts-voice-design and select a narrator before starting the worker."
        )
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    return settings


def load_voice_profiles(settings: Settings) -> dict[str, VoiceProfile]:
    """Load the local, editable mapping from stable product slot to source WAV.

    The database stores only a slot/version snapshot.  Changing a reference WAV
    is therefore a deliberate TTS-machine operation, not a reader API change.
    """
    config_path = settings.voice_slots_path.resolve()
    if not config_path.is_file():
        raise RuntimeError(f"TTS voice-slot configuration is missing: {config_path}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"TTS voice-slot configuration is invalid JSON: {config_path}") from error

    version = raw.get("version")
    voices = raw.get("voices")
    if not isinstance(version, str) or not version.strip() or not isinstance(voices, list):
        raise RuntimeError("TTS voice-slot configuration must contain a version and voices array")

    profiles: dict[str, VoiceProfile] = {}
    for item in voices:
        if not isinstance(item, dict):
            raise RuntimeError("Every TTS voice-slot entry must be an object")
        slot = item.get("slot")
        label = item.get("label")
        source = item.get("reference_wav_path")
        if slot not in VOICE_SLOT_IDS or not isinstance(label, str) or not label.strip() or not isinstance(source, str):
            raise RuntimeError("TTS voice-slot entries require a supported slot, label, and reference_wav_path")
        if slot in profiles:
            raise RuntimeError(f"TTS voice slot is configured more than once: {slot}")
        reference_wav_path = (config_path.parent / source).resolve()
        if not reference_wav_path.is_file():
            raise RuntimeError(f"Reference WAV for TTS voice slot {slot} is missing: {reference_wav_path}")
        profiles[slot] = VoiceProfile(slot=slot, label=label.strip(), version=version.strip(), reference_wav_path=reference_wav_path)

    missing = set(VOICE_SLOT_IDS) - set(profiles)
    if missing:
        raise RuntimeError(f"TTS voice-slot configuration is missing required slots: {', '.join(sorted(missing))}")
    return profiles
