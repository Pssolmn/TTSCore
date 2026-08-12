from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

# Same logger name worker.py uses -- the GUI's QueueLogHandler is already
# attached to it, so warnings from here show up in the log panel for free.
LOGGER = logging.getLogger("readji_tts")

_LABEL_RE = re.compile(r"^(.+)_(\d+)$")


@dataclass(frozen=True)
class VoiceVariant:
    category: str
    index: int
    wav_path: Path


@dataclass(frozen=True)
class VoiceVariantCategory:
    name: str                       # exact on-disk folder name
    folder_path: Path
    variants: dict[int, VoiceVariant]   # index -> variant, only cleanly-parsed entries
    skipped_files: tuple[str, ...]      # candidate .wav files that didn't match


@dataclass(frozen=True)
class VoiceVariantRegistry:
    root: Path
    categories: dict[str, VoiceVariantCategory]


def parse_variant_label(label: str) -> tuple[str, int] | None:
    """Split the trailing `_<digits>` suffix off a label as its index.

    Greedy matching means embedded underscore-number groups earlier in the
    label are preserved as part of the category name: "M_handsome_1_2_3_10"
    -> ("M_handsome_1_2_3", 10), not naively split on the first/last
    underscore. Returns None if the label has no numeric suffix at all.
    """
    match = _LABEL_RE.match(label)
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def _gender_prefix(category: str) -> str | None:
    """'M' or 'F' from a leading 'M_'/'F_' (case-insensitive); None otherwise."""
    upper = category.upper()
    if upper.startswith("M_"):
        return "M"
    if upper.startswith("F_"):
        return "F"
    return None


def _find_category(registry: VoiceVariantRegistry, name: str) -> VoiceVariantCategory | None:
    """Case-insensitive lookup by on-disk folder name."""
    target = name.casefold()
    for category in registry.categories.values():
        if category.name.casefold() == target:
            return category
    return None


def scan_voice_variants(root: Path) -> VoiceVariantRegistry:
    """Scan `root` for one subfolder per voice category.

    No caching -- this is cheap local filesystem I/O (a handful of folders,
    a handful of files), meant to be re-run on every explicit Refresh click,
    matching load_basic_voice_profiles()'s existing pattern for the fixed
    3-slot case. A missing root is deliberately NOT an error (unlike
    load_basic_voice_profiles' required Basic folder): this is optional
    browse-only infrastructure, and the folder genuinely may not exist yet on
    a given machine, so Settings still has to open cleanly.
    """
    if not root.is_dir():
        return VoiceVariantRegistry(root=root, categories={})

    categories: dict[str, VoiceVariantCategory] = {}
    for folder in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name):
        variants: dict[int, VoiceVariant] = {}
        skipped: list[str] = []
        candidates = sorted(
            (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".wav"),
            key=lambda p: p.name,
        )
        for file in candidates:
            parsed = parse_variant_label(file.stem)
            if parsed is None or parsed[0].casefold() != folder.name.casefold():
                skipped.append(file.name)
                LOGGER.warning("voice_variant_skipped_file category=%s file=%s", folder.name, file.name)
                continue
            index = parsed[1]
            if index in variants:
                skipped.append(file.name)
                LOGGER.warning(
                    "voice_variant_duplicate_index category=%s index=%s file=%s", folder.name, index, file.name
                )
                continue
            variants[index] = VoiceVariant(category=folder.name, index=index, wav_path=file)
        categories[folder.name] = VoiceVariantCategory(
            name=folder.name, folder_path=folder, variants=variants, skipped_files=tuple(skipped)
        )
    return VoiceVariantRegistry(root=root, categories=categories)


def resolve_variant(registry: VoiceVariantRegistry, label: str) -> VoiceVariant:
    """Resolve a "<category>_<index>" label to an actual file.

    Falls back to "<gender>_Extra" (same index number) only when the
    category itself doesn't exist -- never by guessing/fuzzy-matching a
    misspelled category name, and never as a substitute for a missing index
    within a category that does exist (that's a distinct, louder error).
    Always raises RuntimeError rather than returning None, matching this
    codebase's convention of plain RuntimeError everywhere with no custom
    exception types -- "no further fallback" means a hard, clearly-logged
    failure, not a silently swallowed one.
    """
    parsed = parse_variant_label(label)
    if parsed is None:
        raise RuntimeError(f"Voice variant label is not in '<category>_<index>' format: {label!r}")
    category_name, index = parsed

    category = _find_category(registry, category_name)
    if category is not None:
        variant = category.variants.get(index)
        if variant is not None:
            return variant
        raise RuntimeError(
            f"Voice variant category {category_name!r} has no index {index} "
            f"(available: {sorted(category.variants)})"
        )

    gender = _gender_prefix(category_name)
    if gender is None:
        raise RuntimeError(
            f"Voice variant category {category_name!r} not found, and it has no M_/F_ prefix to fall back from"
        )
    fallback_name = f"{gender}_Extra"
    fallback = _find_category(registry, fallback_name)
    if fallback is None:
        raise RuntimeError(
            f"Voice variant category {category_name!r} not found; fallback {fallback_name!r} does not exist either"
        )
    variant = fallback.variants.get(index)
    if variant is None:
        raise RuntimeError(
            f"Voice variant category {category_name!r} not found; fallback {fallback_name!r} "
            f"has no index {index} either (available: {sorted(fallback.variants)})"
        )
    LOGGER.warning("voice_variant_fallback label=%s category=%s -> %s", label, category_name, fallback_name)
    return variant
