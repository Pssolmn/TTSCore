"""Validate a frozen Pro job plan and choose one local reference WAV per block.

This module deliberately knows nothing about the Novel Platform schema.  The
API snapshots primitive ``voice_category``, ``voice_index`` and
``voice_shared`` values on the job; this code validates only that contract.
That keeps a queued job deterministic even if the writer later edits aliases.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schemas import NovelBlock
from .voice_resolution import resolve_voice_reference


@dataclass(frozen=True)
class VoiceAssignment:
    voice_category: str | None
    voice_index: int | None
    voice_shared: bool = False


def parse_voice_assignments(value: Any) -> dict[int, VoiceAssignment]:
    """Decode the JSONB snapshot and reject loose or malformed input early."""
    if not isinstance(value, dict):
        raise RuntimeError("Pro job voice_assignments must be an object keyed by slots 0-6")
    parsed: dict[int, VoiceAssignment] = {}
    for raw_slot, raw_assignment in value.items():
        if not isinstance(raw_slot, str) or not raw_slot.isdigit():
            raise RuntimeError("Pro job voice_assignments has an invalid slot key")
        slot = int(raw_slot)
        if slot < 0 or slot > 6 or slot in parsed:
            raise RuntimeError(f"Pro job voice_assignments has invalid slot {raw_slot!r}")
        if not isinstance(raw_assignment, dict):
            raise RuntimeError(f"Pro job voice_assignments slot {slot} must be an object")
        category = raw_assignment.get("voice_category")
        index = raw_assignment.get("voice_index")
        shared = raw_assignment.get("voice_shared", False)
        if category is not None and (not isinstance(category, str) or not category.strip()):
            raise RuntimeError(f"Pro job voice_assignments slot {slot} has an invalid voice_category")
        if index is not None and (isinstance(index, bool) or not isinstance(index, int) or index < 1):
            raise RuntimeError(f"Pro job voice_assignments slot {slot} has an invalid voice_index")
        if not isinstance(shared, bool):
            raise RuntimeError(f"Pro job voice_assignments slot {slot} has an invalid voice_shared flag")
        if (category is None) != (index is None):
            raise RuntimeError(f"Pro job voice_assignments slot {slot} must set both category and index, or neither")
        parsed[slot] = VoiceAssignment(category.strip() if isinstance(category, str) else None, index, shared)
    if 0 not in parsed:
        raise RuntimeError("Pro job voice_assignments is missing narrator slot 0")
    return parsed


def resolve_pro_block_references(
    voices_root: Path,
    blocks: list[NovelBlock],
    assignments: dict[int, VoiceAssignment],
) -> list[Path]:
    """Return one reference WAV per stored block, including gaps for alignment."""
    references: list[Path] = []
    for index, block in enumerate(blocks):
        raw_slot = (block.tts or {}).get("speaker_slot")
        if raw_slot is None:
            slot = 0
        elif isinstance(raw_slot, bool) or not isinstance(raw_slot, int) or raw_slot < 1 or raw_slot > 6:
            raise RuntimeError(f"Pro job block {index + 1} has an invalid speaker_slot")
        else:
            slot = raw_slot
        assignment = assignments.get(slot)
        if assignment is None:
            raise RuntimeError(f"Pro job block {index + 1} uses slot {slot}, but that slot is absent from voice_assignments")
        if slot != 0 and assignment.voice_category is None:
            raise RuntimeError(f"Pro job block {index + 1} uses slot {slot}, but no Pro voice is configured for it")
        references.append(resolve_voice_reference(
            voices_root,
            voice_category=assignment.voice_category,
            voice_index=assignment.voice_index,
            voice_shared=assignment.voice_shared,
        ))
    return references
