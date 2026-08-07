from __future__ import annotations

import logging
from pathlib import Path

from .audio import write_wav
from .config import load_settings
from .provider import VoxCpmNarrator


CANDIDATES = {
    "warm-female": "Warm adult Thai female narrator, clear articulation, calm intimate storytelling, gentle confidence, natural pace",
    "warm-male": "Warm adult Thai male narrator, clear articulation, calm intimate storytelling, gentle confidence, natural pace",
    "neutral": "Professional adult Thai narrator, neutral gender expression, clear pronunciation, composed and immersive novel storytelling",
}

SAMPLE_TEXT = "ค่ำคืนเงียบสงบลงเมื่อสายฝนเริ่มซา แสงไฟจากหน้าต่างไกลออกไปส่องผ่านม่านหมอกบาง และเรื่องราวบทใหม่ก็กำลังจะเริ่มต้นขึ้น"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings(require_master_voice=False)
    output_dir = Path("assets/voice-candidates")
    output_dir.mkdir(parents=True, exist_ok=True)
    narrator = VoxCpmNarrator(settings)
    for name, description in CANDIDATES.items():
        path = output_dir / f"{name}.wav"
        logging.info("creating voice candidate %s", name)
        duration = write_wav(path, narrator.design_voice(SAMPLE_TEXT, description), narrator.sample_rate)
        logging.info("created %s (%.2fs)", path, duration)


if __name__ == "__main__":
    main()
