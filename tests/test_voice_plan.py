from pathlib import Path

import pytest

from readji_tts.schemas import NovelBlock
from readji_tts.voice_plan import parse_voice_assignments, resolve_pro_block_references


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def _assignment_snapshot() -> dict[str, object]:
    return {
        "0": {"voice_category": None, "voice_index": None, "voice_shared": False},
        "1": {"voice_category": "hero_male", "voice_index": 2, "voice_shared": False},
    }


def test_pro_plan_resolves_narrator_and_character_per_block(tmp_path: Path) -> None:
    narrator = _touch(tmp_path / "Basic" / "Basic_female.wav")
    _touch(tmp_path / "hero_male" / "hero_male_1.wav")
    hero = _touch(tmp_path / "hero_male" / "hero_male_2.wav")
    assignments = parse_voice_assignments(_assignment_snapshot())
    blocks = [
        NovelBlock(id="narration", label="paragraph", text="หนึ่ง", style=None),
        NovelBlock(id="hero", label="dialogue", text="สอง", style=None, tts={"speaker_slot": 1}),
    ]

    assert resolve_pro_block_references(tmp_path, blocks, assignments) == [narrator, hero]


def test_pro_plan_rejects_missing_character_assignment(tmp_path: Path) -> None:
    _touch(tmp_path / "Basic" / "Basic_female.wav")
    assignments = parse_voice_assignments({"0": {"voice_category": None, "voice_index": None}})
    blocks = [NovelBlock(id="hero", label="dialogue", text="สอง", style=None, tts={"speaker_slot": 1})]

    with pytest.raises(RuntimeError, match="slot 1"):
        resolve_pro_block_references(tmp_path, blocks, assignments)


@pytest.mark.parametrize("value", [None, {}, {"0": {"voice_category": "hero_male", "voice_index": None}}])
def test_pro_plan_rejects_malformed_snapshots(value: object) -> None:
    with pytest.raises(RuntimeError):
        parse_voice_assignments(value)
