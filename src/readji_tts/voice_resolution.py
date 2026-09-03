"""Resolve one Pro TTS voice assignment to a local reference WAV.

This module intentionally knows nothing about PostgreSQL, a work's schema, or
the render loop.  Its caller supplies the resolved slot assignment directly;
that lets the Novel Platform change how it stores seven slots without changing
the safety-critical filesystem selection rules here.

Pro category convention
-----------------------
Any exact category name may resolve normally. Categories intended to have a
safe missing-folder fallback must end in ``_male`` or ``_female``
(case-insensitive), for example ``handsome_male`` or ``little_female``. That
suffix is the only gender signal used for fallback; the resolver never
fuzzy-matches a typo to a similarly named category. A missing
``pretty_female`` therefore falls back only to ``extra_female``; a missing
ungendered category fails instead of guessing.

Within a category folder, usable files are named ``<category>_<N>.wav`` where
``N`` is a positive integer. Files are ordered by that number. Duplicate
numeric indices (for example ``voice_1.wav`` and ``voice_01.wav``) are rejected
as ambiguous. ``voice_index`` selects by ordered position and wraps around the
files that actually exist.
"""

from __future__ import annotations

import re
from pathlib import Path


DEFAULT_NARRATOR_PATH = Path("Basic") / "Basic_female.wav"
_CATEGORY_GENDER_RE = re.compile(r"_(male|female)$", re.IGNORECASE)
_VARIANT_RE = re.compile(r"^(.+)_(\d+)$")
_BASIC_VARIANTS = ("Basic_old_male.wav", "Basic_young_male.wav", "Basic_female.wav")


def resolve_voice_reference(
    voices_root: Path,
    *,
    voice_category: str | None,
    voice_index: int | None,
    voice_shared: bool = False,
) -> Path:
    """Return the reference WAV for one assigned voice slot.

    ``voice_shared`` is reservation metadata from the writer-facing system.
    It permits several character slots to intentionally share an allocation,
    but it does not alter filesystem resolution, so it is accepted here only
    to keep this function's interface aligned with the upstream assignment.

    Passing no category and no index selects the default narrator
    ``Basic/Basic_female.wav``.  A partly populated assignment is an error:
    silently treating a broken character assignment as narration could make
    an entire scene use the wrong speaker.
    """
    del voice_shared  # Metadata only; see the docstring above.

    category = (voice_category or "").strip()
    if not category:
        if voice_index is not None:
            raise RuntimeError("Voice assignment has voice_index but no voice_category")
        return _require_default_narrator(voices_root)
    if voice_index is None:
        raise RuntimeError(f"Voice assignment for category {category!r} has no voice_index")
    if isinstance(voice_index, bool) or not isinstance(voice_index, int):
        raise RuntimeError(
            f"Voice assignment for category {category!r} has invalid voice_index {voice_index!r}; "
            "expected a positive integer"
        )
    if voice_index < 1:
        raise RuntimeError(f"Voice assignment for category {category!r} has invalid voice_index {voice_index}")

    # Basic is deliberately a fixed three-file tier rather than a dynamic
    # <category>_<N>.wav folder. It is nevertheless a legal Pro narrator
    # override: Basic_1=old_male, Basic_2=young_male, Basic_3=female.
    if category.casefold() == "basic":
        return _select_basic_variant(voices_root, voice_index)

    requested_folder = _find_category_folder(voices_root, category)
    if requested_folder is not None:
        return _select_variant(requested_folder, voice_index, attempted=(category,))

    gender = _gender_from_category(category)
    if gender is None:
        raise RuntimeError(
            f"Voice category {category!r} was not found under {voices_root}, and it does not end in "
            "_male or _female so no extra_<gender> fallback is safe"
        )

    fallback_category = f"extra_{gender}"
    fallback_folder = _find_category_folder(voices_root, fallback_category)
    if fallback_folder is None:
        raise RuntimeError(
            f"Voice category {category!r} was not found under {voices_root}; "
            f"also tried fallback {fallback_category!r}, which does not exist"
        )
    return _select_variant(fallback_folder, voice_index, attempted=(category, fallback_category))


def _require_default_narrator(voices_root: Path) -> Path:
    narrator = voices_root / DEFAULT_NARRATOR_PATH
    if not narrator.is_file():
        raise RuntimeError(f"Default narrator reference WAV is missing: {narrator}")
    return narrator


def _select_basic_variant(voices_root: Path, voice_index: int) -> Path:
    basic_dir = _find_category_folder(voices_root, "Basic")
    if basic_dir is None:
        raise RuntimeError(f"Basic voice category was not found under {voices_root}")
    variants = [basic_dir / name for name in _BASIC_VARIANTS]
    missing = [str(path) for path in variants if not path.is_file()]
    if missing:
        raise RuntimeError("Basic voice category is incomplete; missing reference WAV: " + ", ".join(missing))
    return variants[(voice_index - 1) % len(variants)]


def _find_category_folder(voices_root: Path, category: str) -> Path | None:
    """Find exactly one direct child folder, case-insensitively.

    Scanning child names instead of joining the externally supplied category
    prevents ``../`` or path-separator input from escaping ``voices_root``.
    """
    if not voices_root.is_dir():
        return None
    target = category.casefold()
    for child in voices_root.iterdir():
        if child.is_dir() and child.name.casefold() == target:
            return child
    return None


def _gender_from_category(category: str) -> str | None:
    match = _CATEGORY_GENDER_RE.search(category)
    return match.group(1).lower() if match else None


def _select_variant(folder: Path, voice_index: int, *, attempted: tuple[str, ...]) -> Path:
    variants: list[tuple[int, str, Path]] = []
    filenames_by_index: dict[int, str] = {}
    expected_category = folder.name.casefold()
    for candidate in folder.iterdir():
        if not candidate.is_file() or candidate.suffix.casefold() != ".wav":
            continue
        parsed = _VARIANT_RE.match(candidate.stem)
        if parsed is None or parsed.group(1).casefold() != expected_category:
            continue
        numeric_index = int(parsed.group(2))
        if numeric_index >= 1:
            previous = filenames_by_index.get(numeric_index)
            if previous is not None:
                raise RuntimeError(
                    f"Voice category folder {folder} has duplicate numeric index {numeric_index}: "
                    f"{previous!r} and {candidate.name!r}"
                )
            filenames_by_index[numeric_index] = candidate.name
            variants.append((numeric_index, candidate.name.casefold(), candidate))

    if not variants:
        attempted_text = " -> ".join(repr(item) for item in attempted)
        raise RuntimeError(
            f"Voice category folder {folder} has no usable WAV files after trying {attempted_text}; "
            f"expected {folder.name}_<positive-number>.wav"
        )

    variants.sort(key=lambda item: (item[0], item[1]))
    return variants[(voice_index - 1) % len(variants)][2]
