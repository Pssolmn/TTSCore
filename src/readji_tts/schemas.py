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
            label=str(value.get("label") or "paragraph"),
            text=str(value.get("text") or ""),
            style=value.get("style"),
            tts_text=str(value["tts_text"]) if value.get("tts_text") is not None else None,
            tts=value.get("tts") if isinstance(value.get("tts"), dict) else None,
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


def canonical_blocks(blocks: list[NovelBlock] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    canonical: list[dict[str, Any]] = []
    for item in blocks:
        if isinstance(item, NovelBlock):
            value: dict[str, Any] = {"id": item.id, "label": item.label, "text": item.text, "style": item.style}
            if item.tts_text is not None:
                value["tts_text"] = item.tts_text
            if item.tts is not None:
                value["tts"] = item.tts
            canonical.append(value)
        else:
            value = {
                "id": str(item["id"]),
                "label": str(item.get("label") or "paragraph"),
                "text": str(item.get("text") or ""),
                "style": item.get("style"),
            }
            if item.get("tts_text") is not None:
                value["tts_text"] = str(item["tts_text"])
            if isinstance(item.get("tts"), dict):
                value["tts"] = item["tts"]
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
