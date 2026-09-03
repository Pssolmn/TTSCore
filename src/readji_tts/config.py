from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import socket
import os
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


VOICE_SLOT_IDS = ("old_male", "young_male", "female")

BASIC_VOICE_FOLDER_NAME = "Basic"
BASIC_VOICE_FILE_PREFIX = "Basic_"
SUPPORTED_OUTPUT_SAMPLE_RATES = frozenset({
    8_000, 11_025, 12_000, 16_000, 22_050, 24_000, 32_000, 44_100, 48_000,
})

_BASIC_VOICE_LABELS: dict[str, str] = {
    "old_male": "ชายแก่",
    "young_male": "หนุ่มน้อย",
    "female": "คุณผู้หญิง",
}


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
    # Root folder for both the Basic-tier render voices (see BASIC_VOICE_FOLDER_NAME
    # subfolder, read on every render via load_basic_voice_profiles) and the
    # dynamic per-character voice scan used by Pro rendering and the Settings
    # dialog's voice tray. See voice_resolution.py/voice_render_settings.py.
    voice_variants_path: Path = Field(default=Path("assets/voices"), alias="TTS_VOICE_VARIANTS_PATH")
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
    # Speech is mono.  32 kHz / 32 kbps is deliberately small for published
    # narration; raise either value only when a particular delivery target
    # needs more fidelity.
    output_sample_rate: int = Field(default=32_000, alias="TTS_OUTPUT_SAMPLE_RATE")
    output_mp3_bitrate_kbps: int = Field(default=32, ge=8, le=320, alias="TTS_OUTPUT_MP3_BITRATE_KBPS")
    # This must be explicit before a local GPU worker is allowed to claim a
    # non-local database's jobs. It deliberately lives in the worker's own
    # private .env, not in a frontend/API environment.
    remote_database_confirmed: bool = Field(default=False, alias="TTS_CONFIRM_REMOTE")
    # State is deliberately local and starts at zero when first created. It
    # limits only future TTSCore audio uploads, never scans/counts old R2 data.
    r2_upload_quota_state_path: Path | None = Field(default=None, alias="TTS_R2_UPLOAD_QUOTA_STATE_PATH")
    # Pro jobs can exist in the database before their local reference folders
    # are populated. Keep the worker inert for that tier until an operator
    # explicitly opts in after checking the files.
    pro_render_enabled: bool = Field(default=False, alias="TTS_PRO_RENDER_ENABLED")

    @field_validator("device")
    @classmethod
    def cuda_only(cls, value: str) -> str:
        if value != "cuda":
            raise ValueError("TTS_DEVICE must be cuda; CPU rendering is intentionally unsupported")
        return value

    @field_validator("output_sample_rate")
    @classmethod
    def supported_output_sample_rate(cls, value: int) -> int:
        if value not in SUPPORTED_OUTPUT_SAMPLE_RATES:
            allowed = ", ".join(str(rate) for rate in sorted(SUPPORTED_OUTPUT_SAMPLE_RATES))
            raise ValueError(f"TTS_OUTPUT_SAMPLE_RATE must be one of: {allowed}")
        return value

    @field_validator("r2_public_url")
    @classmethod
    def trim_public_url(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def voice_basic_path(self) -> Path:
        return self.voice_variants_path / BASIC_VOICE_FOLDER_NAME

    @property
    def r2_upload_quota_path(self) -> Path:
        return self.r2_upload_quota_state_path or self.work_dir / "r2-upload-quota.json"


def resolve_env_file() -> Path:
    """Resolve this worker deployment's .env file, without loading it.

    The worker must never borrow credentials from the sibling Novel Platform
    checkout.  That prevented a clean customer handoff and could make one
    deployment claim another deployment's jobs.  The GUI uses this same
    resolver so the displayed file always matches the running process.
    """
    configured_env_file = os.environ.get("TTS_ENV_FILE")
    return Path(configured_env_file) if configured_env_file else Path(".env")


def describe_database_target(database_url: str) -> str:
    """Return the DB target with credentials stripped, safe to display in the GUI."""
    parsed = urlsplit(database_url)
    host = parsed.hostname or "unknown-host"
    port = f":{parsed.port}" if parsed.port else ""
    database = parsed.path.lstrip("/") or "unknown-db"
    return f"{parsed.scheme}://{host}{port}/{database}"


_LOCAL_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1"}


def guard_remote_database(database_url: str, *, confirmed: bool = False) -> None:
    """Refuse to claim/render jobs against a non-local database unless confirmed.

    Phase A (see PHASE_B_PLAN.md) connects straight to Postgres with full
    credentials -- a stray or stale DATABASE_URL pointing at Railway/production
    is a real way for this local machine to start claiming and rendering live
    jobs by accident (this exact class of bug already happened once with a
    different service's DATABASE_URL during dev). TTS_CONFIRM_REMOTE=1 is the
    deliberate escape hatch for when a non-local target really is the intent
    (e.g. this machine has become the production renderer).
    """
    host = (urlsplit(database_url).hostname or "").lower()
    if host in _LOCAL_DATABASE_HOSTS:
        return
    # Keep the process-environment form for existing service launchers, but
    # also honour Settings so TTS_CONFIRM_REMOTE=1 in the worker's .env works.
    if confirmed or os.environ.get("TTS_CONFIRM_REMOTE") == "1":
        return
    target = describe_database_target(database_url)
    raise RuntimeError(
        f"Refusing to start: DATABASE_URL targets a non-local database ({target}). "
        "If this is intentional, set TTS_CONFIRM_REMOTE=1 in your .env to confirm."
    )


def load_settings(*, require_master_voice: bool = False) -> Settings:
    local_env_file = resolve_env_file()
    settings = Settings(_env_file=local_env_file)
    configured_ffmpeg = Path(settings.ffmpeg_path)
    candidates = [
        configured_ffmpeg if configured_ffmpeg.is_file() else None,
        Path(shutil.which(settings.ffmpeg_path)) if shutil.which(settings.ffmpeg_path) else None,
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


def load_basic_voice_profiles(basic_dir: Path) -> dict[str, VoiceProfile]:
    """Load the fixed 3-slot Basic-tier render voices from a real folder on disk.

    Basic tier always renders a single narrator voice; the file for each slot
    lives at ``{basic_dir}/{BASIC_VOICE_FILE_PREFIX}{slot}.wav`` (e.g.
    ``Basic_female.wav``). version is hardcoded to "v1" here: it is a
    cross-repo contract with apps/api's TTS_VOICE_PROFILE_VERSION, and a
    mismatch fails every render job's version check.
    """
    resolved_dir = basic_dir.resolve()
    if not resolved_dir.is_dir():
        raise RuntimeError(f"Basic voice folder is missing: {resolved_dir}")

    profiles: dict[str, VoiceProfile] = {}
    for slot in VOICE_SLOT_IDS:
        reference_wav_path = resolved_dir / f"{BASIC_VOICE_FILE_PREFIX}{slot}.wav"
        if not reference_wav_path.is_file():
            raise RuntimeError(f"Reference WAV for TTS voice slot {slot} is missing: {reference_wav_path}")
        profiles[slot] = VoiceProfile(
            slot=slot,
            label=_BASIC_VOICE_LABELS.get(slot, slot),
            version="v1",
            reference_wav_path=reference_wav_path,
        )
    return profiles
