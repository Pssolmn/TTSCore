from __future__ import annotations

import argparse
import logging
from collections.abc import Mapping
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .audio import write_wav
from .config import VoiceProfile, load_basic_voice_profiles, resolve_env_file
from .provider import VoxCpmNarrator


PREVIEW_TEXT = "เมื่อยามอรุณเบิกฟ้า ก็ถึงเวลาที่ต้องมานั่งอ่านนิยายที่รี้ดจิ"
TTSCORE_ROOT = Path(__file__).resolve().parents[2]
BASIC_VOICE_DIRECTORY = TTSCORE_ROOT / "assets" / "voices" / "Basic"
WEB_PREVIEW_DIRECTORY = (
    Path(__file__).resolve().parents[3]
    / "Novel Platform"
    / "apps"
    / "web"
    / "public"
    / "audio"
    / "tts-samples"
    / "basic"
)


class BasicPreviewSettings(BaseSettings):
    """Only the local VoxCPM options needed to generate web preview WAVs.

    In particular, this deliberately does not require DATABASE_URL or R2
    credentials: preview generation neither reads nor writes those services.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    model_id: str = Field(default="openbmb/VoxCPM2", alias="TTS_MODEL_ID")
    device: str = Field(default="cuda", alias="TTS_DEVICE")
    load_denoiser: bool = Field(default=False, alias="TTS_LOAD_DENOISER")
    optimize: bool = Field(default=False, alias="TTS_OPTIMIZE")
    inference_timesteps: int = Field(default=10, ge=1, le=50, alias="TTS_INFERENCE_TIMESTEPS")
    cfg_value: float = Field(default=2.0, ge=0.0, le=10.0, alias="TTS_CFG_VALUE")
    badcase_max_attempts: int = Field(default=2, ge=1, le=3, alias="TTS_BADCASE_MAX_ATTEMPTS")


def load_basic_preview_settings() -> BasicPreviewSettings:
    return BasicPreviewSettings(_env_file=resolve_env_file())


def preview_paths(output_dir: Path, profiles: Mapping[str, VoiceProfile]) -> dict[str, Path]:
    """Return the fixed web asset path for every loaded Basic voice slot.

    These files are generated demonstrations only.  The actual reference WAVs
    remain in TTSCore so later episode renders keep using the original source.
    """
    return {slot: output_dir / f"{slot}.wav" for slot in profiles}


def generate_basic_previews(*, text: str, output_dir: Path) -> dict[str, tuple[Path, float]]:
    """Generate one local, web-bundled preview WAV per Basic source voice.

    No database, job queue, R2 upload, or reader content is touched.  Existing
    generated preview files at ``output_dir`` are intentionally replaced; the
    source files in ``assets/voices/Basic`` are never written by this command.
    """
    settings = load_basic_preview_settings()
    profiles = load_basic_voice_profiles(BASIC_VOICE_DIRECTORY)
    paths = preview_paths(output_dir, profiles)
    output_dir.mkdir(parents=True, exist_ok=True)

    narrator = VoxCpmNarrator(settings)
    generated: dict[str, tuple[Path, float]] = {}
    for slot, profile in profiles.items():
        path = paths[slot]
        logging.info("generating_basic_preview slot=%s output=%s", slot, path)
        audio = narrator.synthesize(text, profile.reference_wav_path)
        generated[slot] = (path, write_wav(path, audio, narrator.sample_rate))
    return generated


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate local web previews from the 3 Basic reference voices.")
    parser.add_argument("--text", default=PREVIEW_TEXT, help="Thai text to speak in every preview.")
    parser.add_argument("--output-dir", type=Path, default=WEB_PREVIEW_DIRECTORY, help="Web static directory for generated WAVs.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    generated = generate_basic_previews(text=args.text, output_dir=args.output_dir)
    for slot, (path, duration) in generated.items():
        logging.info("generated_basic_preview slot=%s path=%s duration=%.2fs", slot, path, duration)


if __name__ == "__main__":
    main()
