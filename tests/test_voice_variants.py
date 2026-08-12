from pathlib import Path

import pytest

from readji_tts.voice_variants import parse_variant_label, resolve_variant, scan_voice_variants


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


# ---- parse_variant_label ---------------------------------------------------


def test_parse_variant_label_splits_trailing_index() -> None:
    assert parse_variant_label("M_handsome_1") == ("M_handsome", 1)


def test_parse_variant_label_keeps_embedded_underscore_number_groups_in_category() -> None:
    # Only the FINAL _<digits> suffix is the index, even when the category
    # name itself already contains underscore-number groups.
    assert parse_variant_label("M_handsome_1_2_3_10") == ("M_handsome_1_2_3", 10)


def test_parse_variant_label_returns_none_without_a_numeric_suffix() -> None:
    assert parse_variant_label("no_number") is None
    assert parse_variant_label("nounderscore") is None


# ---- scan_voice_variants ----------------------------------------------------


def test_scan_voice_variants_missing_root_returns_empty_registry(tmp_path: Path) -> None:
    registry = scan_voice_variants(tmp_path / "does_not_exist")
    assert registry.categories == {}


def test_scan_voice_variants_finds_matching_files_and_skips_malformed_ones(tmp_path: Path) -> None:
    root = tmp_path / "voices"
    _touch(root / "M_test" / "M_test_1.wav")
    _touch(root / "M_test" / "M_test_2.wav")
    _touch(root / "M_test" / "badname.wav")  # no numeric suffix at all

    registry = scan_voice_variants(root)

    category = registry.categories["M_test"]
    assert sorted(category.variants) == [1, 2]
    assert category.skipped_files == ("badname.wav",)


def test_scan_voice_variants_matches_filenames_case_insensitively(tmp_path: Path) -> None:
    root = tmp_path / "voices"
    _touch(root / "M_test" / "M_TEST_3.wav")

    registry = scan_voice_variants(root)

    assert 3 in registry.categories["M_test"].variants


def test_scan_voice_variants_skips_files_belonging_to_a_different_category(tmp_path: Path) -> None:
    root = tmp_path / "voices"
    _touch(root / "M_test" / "other_prefix_1.wav")

    registry = scan_voice_variants(root)

    category = registry.categories["M_test"]
    assert category.variants == {}
    assert category.skipped_files == ("other_prefix_1.wav",)


def test_scan_voice_variants_keeps_first_file_on_duplicate_index(tmp_path: Path) -> None:
    root = tmp_path / "voices"
    # Two genuinely distinct files (not just a case difference -- Windows
    # filesystems are case-insensitive, so "M_test_1.wav" and "M_TEST_1.wav"
    # would actually be the same file on disk) that both parse to index 1.
    _touch(root / "M_test" / "M_test_1.wav")
    _touch(root / "M_test" / "M_test_01.wav")

    registry = scan_voice_variants(root)

    category = registry.categories["M_test"]
    assert list(category.variants) == [1]
    assert len(category.skipped_files) == 1


def test_scan_voice_variants_lists_an_empty_category_folder(tmp_path: Path) -> None:
    root = tmp_path / "voices"
    (root / "M_empty").mkdir(parents=True)

    registry = scan_voice_variants(root)

    assert registry.categories["M_empty"].variants == {}


# ---- resolve_variant --------------------------------------------------------


def test_resolve_variant_finds_an_existing_file(tmp_path: Path) -> None:
    root = tmp_path / "voices"
    _touch(root / "M_handsome" / "M_handsome_1.wav")
    registry = scan_voice_variants(root)

    variant = resolve_variant(registry, "M_handsome_1")

    assert variant.category == "M_handsome"
    assert variant.index == 1


def test_resolve_variant_raises_when_index_missing_in_an_existing_category(tmp_path: Path) -> None:
    root = tmp_path / "voices"
    _touch(root / "M_handsome" / "M_handsome_1.wav")
    registry = scan_voice_variants(root)

    with pytest.raises(RuntimeError, match="no index 5"):
        resolve_variant(registry, "M_handsome_5")


def test_resolve_variant_falls_back_to_gender_extra_when_category_missing(tmp_path: Path) -> None:
    root = tmp_path / "voices"
    _touch(root / "F_Extra" / "F_Extra_1.wav")
    registry = scan_voice_variants(root)

    # Typo'd/unregistered category "F_Cutee" -- falls back to F_Extra at the
    # same index, never fuzzy-matched to anything else.
    variant = resolve_variant(registry, "F_Cutee_1")

    assert variant.category == "F_Extra"
    assert variant.index == 1


def test_resolve_variant_raises_when_category_and_fallback_both_missing(tmp_path: Path) -> None:
    root = tmp_path / "voices"
    registry = scan_voice_variants(root)

    with pytest.raises(RuntimeError, match="F_Extra"):
        resolve_variant(registry, "F_Cutee_1")


def test_resolve_variant_raises_on_unparseable_label(tmp_path: Path) -> None:
    root = tmp_path / "voices"
    registry = scan_voice_variants(root)

    with pytest.raises(RuntimeError, match="format"):
        resolve_variant(registry, "no_numeric_suffix")
