from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import hashlib
import json


@dataclass(frozen=True)
class NovelBlock:
    id: str
    label: str
    text: str
    style: dict[str, Any] | None
    tts_text: str | None = None
    tts: dict[str, Any] | None = None

    @classmethod
    def from_db(cls, value: dict[str, Any]) -> "NovelBlock":
        return cls(
            id=str(value["id"]),
            # display_label is the current API field. label remains a fallback
            # for episodes authored before the 2026-08-16 field rename.
            label=str(value.get("display_label") or value.get("label") or "paragraph"),
            text=str(value.get("text") or ""),
            style=value.get("style"),
            tts_text=str(value["tts_text"]) if value.get("tts_text") is not None else None,
            tts=_normalize_tts(value.get("tts")),
        )


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    ep_id: int
    request_id: int
    voice_slot: str
    voice_profile_version: str
    source_hash: str
    attempt_count: int
    max_attempts: int
    blocks: list[NovelBlock]
    # JSONB snapshot, kept raw at the DB boundary. voice_plan.py validates it
    # only for a Pro render so legacy Basic jobs remain byte-for-byte valid.
    voice_assignments: dict[str, Any] | None = None
    # Display-only enrichment for the GUI's status line/log. None when the
    # LEFT JOIN in claim_next() finds nothing or is skipped after a failure —
    # callers must treat these as optional and omit them gracefully.
    work_title: str | None = None
    ep_name: str | None = None
    ep_no: int | None = None


@dataclass(frozen=True)
class BlockTimestamp:
    block_id: str
    start: float
    end: float


def _normalize_tts(value: Any) -> dict[str, Any] | None:
    """Map current TTS metadata back to the stable hash/render shape.

    The API renamed ``tts.kind`` to ``tts.block_kind``.  The worker keeps the
    older ``kind`` spelling internally so its pause planner and source hash
    remain compatible with jobs created before that rename.
    """
    if not isinstance(value, dict):
        return None
    normalized = {key: item for key, item in value.items() if key != "block_kind"}
    block_kind = value.get("block_kind")
    if block_kind is not None:
        normalized["kind"] = block_kind
    return normalized


def canonical_blocks(blocks: list[NovelBlock] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    canonical: list[dict[str, Any]] = []
    for item in blocks:
        if isinstance(item, NovelBlock):
            value: dict[str, Any] = {"id": item.id, "label": item.label, "text": item.text, "style": item.style}
            if item.tts_text is not None:
                value["tts_text"] = item.tts_text
            if item.tts is not None:
                value["tts"] = _normalize_tts(item.tts)
            canonical.append(value)
        else:
            # Keep this byte-for-byte compatible with canonicalBlocks() in
            # Novel Platform's tts.service.ts.  In particular, DB rows now
            # use display_label/tts.block_kind while the API deliberately
            # hashes their old label/kind representation for backward
            # compatibility with existing reader audio.
            value: dict[str, Any] = {
                "id": str(item["id"]),
                "text": str(item.get("text") or ""),
            }
            display_label = item.get("display_label")
            legacy_label = item.get("label")
            if display_label is not None or legacy_label is not None:
                value["label"] = str(display_label if display_label is not None else legacy_label)
            if "style" in item:
                value["style"] = item["style"]
            if "tts_text" in item:
                tts_text = item["tts_text"]
                value["tts_text"] = str(tts_text) if tts_text is not None else None
            if "tts" in item:
                raw_tts = item["tts"]
                value["tts"] = _normalize_tts(raw_tts) if isinstance(raw_tts, dict) else raw_tts
            canonical.append(value)
    return canonical


def source_hash(blocks: list[NovelBlock] | list[dict[str, Any]]) -> str:
    # Must remain byte-for-byte equivalent to apps/api/src/modules/tts/tts.service.ts.
    serialized = json.dumps(canonical_blocks(blocks), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def with_timestamps(blocks: list[dict[str, Any]], timestamps: list[BlockTimestamp]) -> list[dict[str, Any]]:
    by_id = {timestamp.block_id: timestamp for timestamp in timestamps}
    rendered: list[dict[str, Any]] = []
    for block in blocks:
        timestamp = by_id.get(str(block.get("id")))
        if timestamp is None:
            raise ValueError(f"Missing timestamp for block {block.get('id')}")
        rendered.append(
            {
                **block,
                "audio_ts": {
                    "start": round(timestamp.start, 6),
                    "end": round(timestamp.end, 6),
                },
            }
        )
    return rendered
