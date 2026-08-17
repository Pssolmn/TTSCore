from pathlib import Path

from readji_tts.basic_preview import BasicPreviewSettings, preview_paths
from readji_tts.config import VoiceProfile


def test_preview_paths_are_web_assets_and_do_not_replace_reference_files(tmp_path: Path) -> None:
    source = tmp_path / "Basic_female.wav"
    output = tmp_path / "web-static" / "basic"
    profiles = {
        "female": VoiceProfile(slot="female", label="female", version="v1", reference_wav_path=source),
        "old_male": VoiceProfile(slot="old_male", label="old", version="v1", reference_wav_path=tmp_path / "Basic_old_male.wav"),
    }

    assert preview_paths(output, profiles) == {
        "female": output / "female.wav",
        "old_male": output / "old_male.wav",
    }
    assert preview_paths(output, profiles)["female"] != source


def test_preview_settings_do_not_need_database_or_r2_secrets() -> None:
    settings = BasicPreviewSettings()

    assert settings.device == "cuda"
    assert settings.model_id == "openbmb/VoxCPM2"
