from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Iterable, Sequence
import unicodedata

import numpy as np
import soundfile as sf

from .schemas import BlockTimestamp, NovelBlock


ELLIPSIS_PATTERN = re.compile(r"(?:\.{3,}|…+)")
SCENE_BREAK_PATTERN = re.compile(r"^\s*(?:\*\s*){1,}$")
ELLIPSIS_PAUSE_SECONDS = 0.55
SCENE_BREAK_PAUSE_SECONDS = 1.25
SUPPORTED_TTS_CHARACTER = re.compile(r"[A-Za-z0-9\u0E00-\u0E7F\s.,!?;:'\"()\[\]{}…—–\-_/+=&#%@]")
# Character aliases may be Thai or ASCII. Core numeric directives keep the
# same form; every matched alias is metadata and must never be pronounced.
EDITOR_DIRECTIVE = re.compile(r"//([A-Za-z0-9_\-\u0E00-\u0E7F]+)|\{(pause\s*:\s*[0-9]+(?:\.[0-9]+)?|skip|breath)\}", re.IGNORECASE)
EDITOR_CORE_PAUSES = {"1": 0.25, "2": 0.5, "3": 1.0, "4": 2.0, "6": 0.25}


@dataclass(frozen=True)
class SynthesisSegment:
    """One speakable phrase or an intentional silent pause in a novel block."""

    text: str | None = None
    silence_seconds: float = 0.0


def _supported_characters_only(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return "".join(char if SUPPORTED_TTS_CHARACTER.fullmatch(char) else " " for char in normalized)


def sanitize_tts_text(text: str) -> str:
    """Keep Thai, English, digits, and safe narration punctuation only.

    Import/test content can contain CJK, Korean, emoji, or other scripts that
    VoxCPM2 cannot narrate reliably. The original novel text remains untouched
    in PostgreSQL; this filter applies only to the synthesis input.
    """
    # Asterisks are visual markup/scene separators in novel content. They are
    # never narration text: `plan_tts_segments` handles a standalone divider as
    # silence, while inline markup is simply removed here.
    cleaned = _supported_characters_only(text).replace("*", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _plan_plain_text(text: str) -> list[SynthesisSegment]:
    """Convert reader text into spoken phrases and deterministic pauses.

    The model is not asked to pronounce decorative asterisks or ellipses. A
    line containing only `*` characters becomes a scene-break silence; `...`
    and `…` within prose become a short pause while preserving the words on
    both sides. This happens before chunking so a pause cannot be split away.
    """
    normalized = unicodedata.normalize("NFKC", text)
    if SCENE_BREAK_PATTERN.fullmatch(normalized):
        return [SynthesisSegment(silence_seconds=SCENE_BREAK_PAUSE_SECONDS)]

    filtered = _supported_characters_only(normalized)
    segments: list[SynthesisSegment] = []
    cursor = 0
    for match in ELLIPSIS_PATTERN.finditer(filtered):
        spoken = sanitize_tts_text(filtered[cursor:match.start()])
        if spoken:
            segments.append(SynthesisSegment(text=spoken))
        # Keep a pause even at the beginning/end of a block. It preserves the
        # author's intended beat without asking the model to read punctuation.
        segments.append(SynthesisSegment(silence_seconds=ELLIPSIS_PAUSE_SECONDS))
        cursor = match.end()

    spoken = sanitize_tts_text(filtered[cursor:])
    if spoken:
        segments.append(SynthesisSegment(text=spoken))
    return segments


def plan_tts_segments(text: str, tts: dict[str, object] | None = None) -> list[SynthesisSegment]:
    """Convert editor source into spoken phrases and deterministic pauses.

    Text can include TTS-only directives. They are stripped before model
    inference: //1–//4 and //6 add pacing, //5/{skip}/none skip the block,
    {pause:N} adds silence, and //character is metadata for a later
    character-voice stage. A gap row never asks the model to speak.
    """
    config = tts or {}
    if config.get("skip") or text.strip().lower() == "none":
        return []
    if config.get("kind") == "gap":
        duration = float(config.get("gap_seconds") or 1.0)
        return [SynthesisSegment(silence_seconds=max(0.1, min(duration, 15.0)))]

    segments: list[SynthesisSegment] = []
    cursor = 0
    for match in EDITOR_DIRECTIVE.finditer(text):
        segments.extend(_plan_plain_text(text[cursor:match.start()]))
        shortcut, brace = match.groups()
        if shortcut is not None:
            if shortcut == "5":
                return []
            pause = EDITOR_CORE_PAUSES.get(shortcut)
            if pause is not None:
                segments.append(SynthesisSegment(silence_seconds=pause))
            # Alpha shortcuts identify a character only. The current narrator
            # still uses its selected slot and must not pronounce the token.
        elif brace is not None:
            directive = brace.replace(" ", "").lower()
            if directive == "skip":
                return []
            if directive == "breath":
                segments.append(SynthesisSegment(silence_seconds=0.25))
            elif directive.startswith("pause:"):
                seconds = float(directive.split(":", 1)[1])
                segments.append(SynthesisSegment(silence_seconds=max(0.1, min(seconds, 15.0))))
        cursor = match.end()
    segments.extend(_plan_plain_text(text[cursor:]))
    return segments


def split_text(text: str, max_chars: int) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]
    chunks: list[str] = []
    remainder = cleaned
    delimiters = re.compile(r"[。！？!?…\n]|(?<=[,;:])\s|\s+")
    while len(remainder) > max_chars:
        candidates = [match.end() for match in delimiters.finditer(remainder[: max_chars + 1])]
        split_at = max(candidates, default=0)
        if split_at < max_chars // 2:
            split_at = remainder.rfind(" ", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = max_chars
        chunks.append(remainder[:split_at].strip())
        remainder = remainder[split_at:].strip()
    if remainder:
        chunks.append(remainder)
    return chunks


def count_render_output_blocks(
    blocks: Iterable[NovelBlock],
    max_chunk_chars: int,
    *,
    inter_block_silence_seconds: float = 0.0,
    inter_block_silence_seconds_by_block: Sequence[float] | None = None,
    stop_after: int | None = None,
) -> int:
    """Count temporary WAVs the worker would create without synthesizing.

    A spoken text chunk and an intentional pause each become one WAV before
    the final MP3 is concatenated.  ``stop_after`` lets the worker stop its
    preflight scan as soon as an enforced limit is exceeded, instead of doing
    avoidable work on a deliberately oversized request.
    """
    block_list = list(blocks)
    output_blocks = 0
    for index, block in enumerate(block_list):
        segments = plan_tts_segments(block.tts_text or block.text, block.tts)
        for segment in segments:
            if segment.text:
                output_blocks += len(split_text(segment.text, max_chunk_chars))
            elif segment.silence_seconds > 0:
                output_blocks += 1
            if stop_after is not None and output_blocks > stop_after:
                return output_blocks
        block_silence = (
            inter_block_silence_seconds_by_block[index]
            if inter_block_silence_seconds_by_block is not None
            else inter_block_silence_seconds
        )
        if should_add_inter_block_silence(segments, index, len(block_list), block_silence):
            output_blocks += 1
            if stop_after is not None and output_blocks > stop_after:
                return output_blocks
    return output_blocks


def should_add_inter_block_silence(
    segments: Iterable[SynthesisSegment],
    block_index: int,
    total_blocks: int,
    inter_block_silence_seconds: float,
) -> bool:
    """Whether a rendered block gets an operator-configured trailing pause."""
    planned = list(segments)
    return (
        inter_block_silence_seconds > 0
        and block_index < total_blocks - 1
        and any(segment.text for segment in planned)
        # Do not double a pause already deliberately written into this block,
        # such as an ending ellipsis or editor pause directive.
        and bool(planned)
        and planned[-1].text is not None
    )


def write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> float:
    waveform = np.asarray(audio, dtype=np.float32)
    if waveform.ndim > 1:
        waveform = waveform.squeeze()
    if waveform.ndim != 1 or waveform.size == 0:
        raise ValueError("VoxCPM2 returned an empty or invalid audio waveform")
    sf.write(path, waveform, sample_rate, subtype="PCM_16")
    return sf.info(path).frames / sample_rate


def write_silence_wav(path: Path, duration_seconds: float, sample_rate: int) -> float:
    if duration_seconds <= 0:
        raise ValueError("Silence duration must be greater than zero")
    frames = max(1, round(duration_seconds * sample_rate))
    sf.write(path, np.zeros(frames, dtype=np.float32), sample_rate, subtype="PCM_16")
    return frames / sample_rate


def concatenate_to_mp3(
    wav_paths: list[Path],
    output_mp3: Path,
    *,
    ffmpeg_path: str,
    output_sample_rate: int = 32_000,
    mp3_bitrate_kbps: int = 32,
) -> float:
    """Concatenate mono WAV blocks into a compact MP3 publication file.

    MP3 is a lossy compressed format and has no meaningful PCM "bit depth".
    ``pcm_s16le`` below is only the 16-bit temporary WAV used by ffmpeg; it is
    deleted before upload.  The published file's size is controlled chiefly by
    ``mp3_bitrate_kbps`` and, secondarily, ``output_sample_rate``.
    """
    if not wav_paths:
        raise ValueError("Episode has no audible blocks")
    output_wav = output_mp3.with_suffix(".wav")
    manifest = output_mp3.with_suffix(".concat.txt")
    manifest.write_text("".join(f"file '{path.resolve().as_posix()}'\n" for path in wav_paths), encoding="utf-8")
    try:
        subprocess.run(
            [
                ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                "-i", str(manifest), "-ar", str(output_sample_rate), "-ac", "1", "-c:a", "pcm_s16le", str(output_wav),
            ],
            check=True,
        )
        subprocess.run(
            [
                ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error", "-i", str(output_wav),
                "-ar", str(output_sample_rate), "-ac", "1", "-c:a", "libmp3lame", "-b:a", f"{mp3_bitrate_kbps}k",
                "-map_metadata", "-1", str(output_mp3),
            ],
            check=True,
        )
        info = sf.info(output_wav)
        return info.frames / info.samplerate
    finally:
        manifest.unlink(missing_ok=True)
        output_wav.unlink(missing_ok=True)


def timestamps_from_durations(
    block_durations: Iterable[tuple[str, float]], *, initial_offset_seconds: float = 0.0
) -> list[BlockTimestamp]:
    cursor = initial_offset_seconds
    timestamps: list[BlockTimestamp] = []
    for block_id, duration in block_durations:
        start = cursor
        cursor += duration
        timestamps.append(BlockTimestamp(block_id=block_id, start=start, end=cursor))
    return timestamps
