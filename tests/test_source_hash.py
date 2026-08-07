from pathlib import Path
import shutil

import numpy as np
import pytest
import soundfile as sf

from readji_tts.audio import (
    ELLIPSIS_PAUSE_SECONDS,
    SCENE_BREAK_PAUSE_SECONDS,
    concatenate_to_mp3,
    count_render_output_blocks,
    plan_tts_segments,
    sanitize_tts_text,
    split_text,
    write_silence_wav,
)
from readji_tts.schemas import NovelBlock, source_hash


def test_source_hash_is_stable_when_object_property_order_changes() -> None:
    left = [{"id": "block_1", "label": "paragraph", "text": "สวัสดี", "style": {"bold": True, "color": "#111111"}}]
    right = [{"text": "สวัสดี", "style": {"color": "#111111", "bold": True}, "label": "paragraph", "id": "block_1", "audio_ts": {"start": 0, "end": 1}}]
    assert source_hash(left) == source_hash(right)


def test_split_text_preserves_all_text() -> None:
    source = "ประโยคแรกจบแล้ว ประโยคที่สองยาวขึ้นเพื่อทดสอบการแบ่งข้อความของ worker"
    chunks = split_text(source, 24)
    assert "".join(chunks).replace(" ", "") == source.replace(" ", "")
    assert all(chunk for chunk in chunks)


def test_sanitize_tts_text_keeps_thai_and_english_but_strips_unsupported_scripts() -> None:
    assert sanitize_tts_text('สวัสดี Readji 2026 안녕하세요 世界!') == 'สวัสดี Readji 2026 !'


def test_plan_tts_segments_turns_ellipses_into_silence_and_strips_inline_asterisks() -> None:
    segments = plan_tts_segments("first... *second*… third")
    assert [(segment.text, segment.silence_seconds) for segment in segments] == [
        ("first", 0),
        (None, ELLIPSIS_PAUSE_SECONDS),
        ("second", 0),
        (None, ELLIPSIS_PAUSE_SECONDS),
        ("third", 0),
    ]


def test_plan_tts_segments_turns_asterisk_only_block_into_scene_break() -> None:
    segments = plan_tts_segments("* * *")
    assert len(segments) == 1
    assert segments[0].text is None
    assert segments[0].silence_seconds == SCENE_BREAK_PAUSE_SECONDS


def test_plan_tts_segments_handles_editor_directives_without_pronouncing_them() -> None:
    segments = plan_tts_segments("ก่อน //2 หลัง {pause:1.5} จบ //hero")
    assert [(segment.text, segment.silence_seconds) for segment in segments] == [
        ("ก่อน", 0),
        (None, 0.5),
        ("หลัง", 0),
        (None, 1.5),
        ("จบ", 0),
    ]


def test_plan_tts_segments_supports_gap_and_skip_metadata() -> None:
    segments = plan_tts_segments("ignored", {"kind": "gap", "gap_seconds": 1.25})
    assert len(segments) == 1
    segment = segments[0]
    assert segment.text is None
    assert segment.silence_seconds == 1.25
    assert plan_tts_segments("ข้อความ", {"skip": True}) == []


def test_output_block_preflight_counts_spoken_chunks_and_pauses() -> None:
    blocks = [
        NovelBlock(id="one", label="paragraph", text="one two three", style=None),
        NovelBlock(id="two", label="paragraph", text="...", style=None),
        NovelBlock(id="three", label="paragraph", text="* * *", style=None),
    ]

    # Two spoken chunks, one ellipsis pause, and one scene-break pause.
    assert count_render_output_blocks(blocks, 7) == 4
    # The preflight can stop immediately after proving a configured limit was
    # exceeded; 4 means "at least 4" here.
    assert count_render_output_blocks(blocks, 7, stop_after=3) == 4


def test_concatenate_to_mp3_preserves_pcm_duration(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg") or "C:/ytdl/ffmpeg.exe"
    if not Path(ffmpeg).is_file():
        pytest.skip("ffmpeg is unavailable on this machine")
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    sf.write(first, np.zeros(4_800, dtype=np.float32), 48_000, subtype="PCM_16")
    sf.write(second, np.zeros(2_400, dtype=np.float32), 48_000, subtype="PCM_16")
    output = tmp_path / "episode.mp3"
    duration = concatenate_to_mp3([first, second], output, ffmpeg_path=ffmpeg)
    assert output.is_file()
    assert duration == pytest.approx(0.15, abs=0.001)


def test_write_silence_wav_has_requested_duration(tmp_path: Path) -> None:
    path = tmp_path / "pause.wav"
    duration = write_silence_wav(path, 0.55, 48_000)
    assert duration == pytest.approx(0.55, abs=0.001)
    assert sf.info(path).frames == 26_400
