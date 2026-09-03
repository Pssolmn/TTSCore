from pathlib import Path

import pytest

from readji_tts.voice_resolution import DEFAULT_NARRATOR_PATH, resolve_voice_reference


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def _default_narrator(root: Path) -> Path:
    path = root / DEFAULT_NARRATOR_PATH
    _touch(path)
    return path


def _variant(root: Path, category: str, index: int) -> Path:
    path = root / category / f"{category}_{index}.wav"
    _touch(path)
    return path


def test_resolve_voice_reference_uses_basic_female_for_an_unassigned_narrator(tmp_path: Path) -> None:
    expected = _default_narrator(tmp_path)

    resolved = resolve_voice_reference(
        tmp_path,
        voice_category=None,
        voice_index=None,
        voice_shared=False,
    )

    assert resolved == expected


def test_resolve_voice_reference_fails_when_default_narrator_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Default narrator"):
        resolve_voice_reference(tmp_path, voice_category=None, voice_index=None)


def test_resolve_voice_reference_resolves_an_existing_category_by_index(tmp_path: Path) -> None:
    _variant(tmp_path, "handsome_male", 1)
    expected = _variant(tmp_path, "handsome_male", 2)

    resolved = resolve_voice_reference(tmp_path, voice_category="handsome_male", voice_index=2)

    assert resolved == expected


def test_resolve_voice_reference_wraps_to_the_real_file_count(tmp_path: Path) -> None:
    expected = _variant(tmp_path, "little_female", 1)
    _variant(tmp_path, "little_female", 4)

    # voice_index=3 chooses the first of two actual files, not a nonexistent
    # literal little_female_3.wav.
    resolved = resolve_voice_reference(tmp_path, voice_category="little_female", voice_index=3)

    assert resolved == expected


def test_resolve_voice_reference_falls_back_to_extra_for_a_missing_gendered_category(tmp_path: Path) -> None:
    _variant(tmp_path, "extra_female", 1)
    expected = _variant(tmp_path, "extra_female", 2)

    resolved = resolve_voice_reference(tmp_path, voice_category="preetty_female", voice_index=2)

    assert resolved == expected


def test_resolve_voice_reference_fallback_wraps_like_any_other_category(tmp_path: Path) -> None:
    expected = _variant(tmp_path, "extra_male", 1)
    _variant(tmp_path, "extra_male", 2)

    resolved = resolve_voice_reference(tmp_path, voice_category="unknown_male", voice_index=3, voice_shared=True)

    assert resolved == expected


def test_resolve_voice_reference_never_fuzzy_matches_a_typo_to_another_category(tmp_path: Path) -> None:
    _variant(tmp_path, "pretty_female", 1)
    expected = _variant(tmp_path, "extra_female", 1)

    resolved = resolve_voice_reference(tmp_path, voice_category="preetty_female", voice_index=1)

    assert resolved == expected
    assert resolved.parent.name == "extra_female"


def test_resolve_voice_reference_fails_when_category_and_gender_fallback_are_missing(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"unknown_female.*extra_female"):
        resolve_voice_reference(tmp_path, voice_category="unknown_female", voice_index=1)


def test_resolve_voice_reference_fails_when_missing_category_has_no_gender_suffix(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"no extra_<gender> fallback"):
        resolve_voice_reference(tmp_path, voice_category="mysterious", voice_index=1)


def test_resolve_voice_reference_fails_when_an_existing_folder_has_no_usable_wav(tmp_path: Path) -> None:
    (tmp_path / "handsome_male").mkdir()
    _touch(tmp_path / "handsome_male" / "wrong_name.wav")

    with pytest.raises(RuntimeError, match="no usable WAV"):
        resolve_voice_reference(tmp_path, voice_category="handsome_male", voice_index=1)


def test_resolve_voice_reference_requires_complete_non_narrator_assignment(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="no voice_index"):
        resolve_voice_reference(tmp_path, voice_category="handsome_male", voice_index=None)
    with pytest.raises(RuntimeError, match="no voice_category"):
        resolve_voice_reference(tmp_path, voice_category=None, voice_index=1)


@pytest.mark.parametrize("invalid_index", [True, False, 1.5, "1"])
def test_resolve_voice_reference_rejects_non_integer_indices(tmp_path: Path, invalid_index: object) -> None:
    _variant(tmp_path, "handsome_male", 1)

    with pytest.raises(RuntimeError, match="positive integer"):
        resolve_voice_reference(
            tmp_path,
            voice_category="handsome_male",
            voice_index=invalid_index,  # type: ignore[arg-type]
        )


def test_resolve_voice_reference_rejects_duplicate_numeric_indices(tmp_path: Path) -> None:
    _variant(tmp_path, "handsome_male", 1)
    _touch(tmp_path / "handsome_male" / "handsome_male_01.wav")

    with pytest.raises(RuntimeError, match="duplicate numeric index 1"):
        resolve_voice_reference(tmp_path, voice_category="handsome_male", voice_index=1)


def test_resolve_voice_reference_allows_a_basic_override_with_wraparound(tmp_path: Path) -> None:
    old_male = tmp_path / "Basic" / "Basic_old_male.wav"
    young_male = tmp_path / "Basic" / "Basic_young_male.wav"
    female = _default_narrator(tmp_path)
    _touch(old_male)
    _touch(young_male)

    assert resolve_voice_reference(tmp_path, voice_category="Basic", voice_index=1) == old_male
    assert resolve_voice_reference(tmp_path, voice_category="basic", voice_index=3) == female
    assert resolve_voice_reference(tmp_path, voice_category="Basic", voice_index=4) == old_male
