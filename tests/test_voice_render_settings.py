from pathlib import Path

import pytest

from readji_tts.voice_render_settings import (
    DEFAULT_TIMING,
    VoiceRenderSettingsStore,
    VoiceRenderTiming,
    scan_voice_reference_files,
)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_new_voice_files_are_scanned_without_a_settings_entry(tmp_path: Path) -> None:
    female = _touch(tmp_path / "Basic" / "Basic_female.wav")
    extra = _touch(tmp_path / "extra_female" / "extra_female_1.wav")
    assert scan_voice_reference_files(tmp_path) == [female, extra]


def test_timing_is_saved_by_portable_path_and_defaults_for_new_voice(tmp_path: Path) -> None:
    female = _touch(tmp_path / "Basic" / "Basic_female.wav")
    new_voice = _touch(tmp_path / "little_female" / "little_female_1.wav")
    store = VoiceRenderSettingsStore(tmp_path)

    store.save_timing(female, VoiceRenderTiming(lead_in_seconds=0.18, inter_block_silence_seconds=0.12))

    reloaded = VoiceRenderSettingsStore(tmp_path)
    assert reloaded.timing_for(female) == VoiceRenderTiming(lead_in_seconds=0.18, inter_block_silence_seconds=0.12)
    assert reloaded.timing_for(new_voice) == DEFAULT_TIMING


def test_timing_rejects_values_outside_safe_range(tmp_path: Path) -> None:
    female = _touch(tmp_path / "Basic" / "Basic_female.wav")
    with pytest.raises(RuntimeError, match="between 0 and 3.0"):
        VoiceRenderSettingsStore(tmp_path).save_timing(female, VoiceRenderTiming(lead_in_seconds=3.1))


def test_timings_for_reads_the_settings_file_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    female = _touch(tmp_path / "Basic" / "Basic_female.wav")
    extra = _touch(tmp_path / "extra_female" / "extra_female_1.wav")
    store = VoiceRenderSettingsStore(tmp_path)
    expected = {store.key_for(female): VoiceRenderTiming(lead_in_seconds=0.2)}
    calls = 0

    def load_once() -> dict[str, VoiceRenderTiming]:
        nonlocal calls
        calls += 1
        return expected

    monkeypatch.setattr(store, "load", load_once)

    assert store.timings_for([female, extra]) == [expected[store.key_for(female)], DEFAULT_TIMING]
    assert calls == 1
