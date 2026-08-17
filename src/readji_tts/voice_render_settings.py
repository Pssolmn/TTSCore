"""Persistent per-reference-WAV timing controls for rendered narration.

The settings file lives alongside the voice folders, but it is never used to
discover or resolve a voice.  Every WAV under ``assets/voices`` is discovered
from the filesystem first; an absent entry simply receives zero added silence.
That means newly copied folders work immediately and only need a settings entry
when an operator chooses to tune their pacing.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any


SETTINGS_FILENAME = "voice-render-settings.json"
_MAX_SECONDS = 3.0


@dataclass(frozen=True)
class VoiceRenderTiming:
    """Extra silence in seconds for one reference WAV."""

    lead_in_seconds: float = 0.0
    inter_block_silence_seconds: float = 0.0


DEFAULT_TIMING = VoiceRenderTiming()


def scan_voice_reference_files(voices_root: Path) -> list[Path]:
    """Return every real WAV below the voice root, sorted by portable key."""
    root = voices_root.resolve()
    if not root.is_dir():
        return []
    files: list[Path] = []
    for candidate in root.rglob("*"):
        if not candidate.is_file() or candidate.suffix.casefold() != ".wav":
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            # Do not follow a symlink out of the configured voice root.
            continue
        files.append(resolved)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix().casefold())


class VoiceRenderSettingsStore:
    """Read/write timing keyed by a WAV's path relative to ``voices_root``."""

    def __init__(self, voices_root: Path) -> None:
        self.voices_root = voices_root.resolve()
        self.path = self.voices_root / SETTINGS_FILENAME

    def key_for(self, wav_path: Path) -> str:
        resolved = wav_path.resolve()
        try:
            relative = resolved.relative_to(self.voices_root)
        except ValueError as error:
            raise RuntimeError(f"Voice WAV is outside the configured voice folder: {resolved}") from error
        if resolved.suffix.casefold() != ".wav":
            raise RuntimeError(f"Voice render settings require a WAV file: {resolved}")
        return relative.as_posix()

    def load(self) -> dict[str, VoiceRenderTiming]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Unable to read voice render settings {self.path}: {error}") from error
        if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("voices"), dict):
            raise RuntimeError(f"Voice render settings {self.path} must contain version=1 and a voices object")

        timings: dict[str, VoiceRenderTiming] = {}
        for key, value in raw["voices"].items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise RuntimeError(f"Voice render settings {self.path} contains an invalid voice entry")
            timings[key] = _timing_from_mapping(value, source=f"{self.path} ({key})")
        return timings

    def timing_for(self, wav_path: Path) -> VoiceRenderTiming:
        return self.load().get(self.key_for(wav_path), DEFAULT_TIMING)

    def save_timing(self, wav_path: Path, timing: VoiceRenderTiming) -> None:
        key = self.key_for(wav_path)
        _validate_timing(timing, source=key)
        timings = self.load()
        if timing == DEFAULT_TIMING:
            timings.pop(key, None)
        else:
            timings[key] = timing

        self.voices_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "voices": {
                saved_key: {
                    "lead_in_seconds": saved_timing.lead_in_seconds,
                    "inter_block_silence_seconds": saved_timing.inter_block_silence_seconds,
                }
                for saved_key, saved_timing in sorted(timings.items())
            },
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)


def _timing_from_mapping(value: dict[str, Any], *, source: str) -> VoiceRenderTiming:
    timing = VoiceRenderTiming(
        lead_in_seconds=_as_seconds(value.get("lead_in_seconds", 0.0), source=source, field="lead_in_seconds"),
        inter_block_silence_seconds=_as_seconds(
            value.get("inter_block_silence_seconds", 0.0), source=source, field="inter_block_silence_seconds"
        ),
    )
    _validate_timing(timing, source=source)
    return timing


def _as_seconds(value: Any, *, source: str, field: str) -> float:
    if isinstance(value, bool):
        raise RuntimeError(f"Voice render setting {field} in {source} must be a number")
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"Voice render setting {field} in {source} must be a number") from error


def _validate_timing(timing: VoiceRenderTiming, *, source: str) -> None:
    for field, value in (
        ("lead_in_seconds", timing.lead_in_seconds),
        ("inter_block_silence_seconds", timing.inter_block_silence_seconds),
    ):
        if not math.isfinite(value) or value < 0 or value > _MAX_SECONDS:
            raise RuntimeError(f"Voice render setting {field} in {source} must be between 0 and {_MAX_SECONDS} seconds")
