"""Exercise the live worker end-to-end with data that this script removes itself."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from readji_tts.config import load_settings
from readji_tts.r2 import R2Storage
from readji_tts.schemas import source_hash


TIMEOUT_SECONDS = 360
POLL_SECONDS = 3


def main() -> None:
    settings = load_settings()
    storage = R2Storage(settings)
    suffix = uuid4().hex
    blocks = [
        {"id": "block_1", "label": "narration", "text": "นี่คือการทดสอบระบบเสียงบรรยายของรีดจิ", "style": None, "audio_ts": None},
        {"id": "block_2", "label": "paragraph", "text": "เมื่อการทดสอบเสร็จ ข้อมูลและไฟล์เสียงนี้จะถูกลบออกโดยอัตโนมัติ", "style": None, "audio_ts": None},
    ]
    work_id: int | None = None
    job_id: int | None = None
    audio_key: str | None = None

    try:
        with psycopg.connect(settings.database_url, row_factory=dict_row) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id FROM users WHERE level >= 6 ORDER BY id LIMIT 1")
                    writer = cursor.fetchone()
                    if writer is None:
                        raise RuntimeError("No writer account exists for the isolated TTS smoke test")
                    cursor.execute(
                        """
                        INSERT INTO works (uuid, title, author_id, type, age_rate, created_by, updated_by)
                        VALUES (%s, %s, %s, 'novel', 'all', %s, %s)
                        RETURNING p_id
                        """,
                        (f"tts-smoke-{suffix}", "[TTS smoke test — auto cleanup]", writer["id"], writer["id"], writer["id"]),
                    )
                    work_id = int(cursor.fetchone()["p_id"])
                    cursor.execute(
                        """
                        INSERT INTO work_ep (p_id, ep_name, ep_no, ep_content, created_by, updated_by)
                        VALUES (%s, 'Smoke test', 1, %s, %s, %s)
                        RETURNING ep_id
                        """,
                        (work_id, Jsonb(blocks), writer["id"], writer["id"]),
                    )
                    episode_id = int(cursor.fetchone()["ep_id"])
                    cursor.execute(
                        """
                        INSERT INTO tts_jobs (ep_id, requested_by, source_hash, status, voice_mode)
                        VALUES (%s, %s, %s, 'pending', 'single')
                        RETURNING id
                        """,
                        (episode_id, writer["id"], source_hash(blocks)),
                    )
                    job_id = int(cursor.fetchone()["id"])
        print(json.dumps({"event": "queued", "job_id": job_id, "work_id": work_id}, ensure_ascii=False))

        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            with psycopg.connect(settings.database_url, row_factory=dict_row) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT job.status, job.audio_key, job.audio_url, job.duration_seconds, episode.ep_content
                        FROM tts_jobs AS job
                        JOIN work_ep AS episode ON episode.ep_id = job.ep_id
                        WHERE job.id = %s
                        """,
                        (job_id,),
                    )
                    result = cursor.fetchone()
            if result is None:
                raise RuntimeError("Smoke-test job disappeared before completion")
            if result["status"] == "done":
                audio_key = str(result["audio_key"])
                if not audio_key or not result["audio_url"] or float(result["duration_seconds"] or 0) <= 0:
                    raise RuntimeError("Completed job is missing a valid audio artifact")
                if any(block.get("audio_ts") is None for block in result["ep_content"]):
                    raise RuntimeError("Completed job is missing one or more block timestamps")
                storage.client.head_object(Bucket=storage.bucket, Key=audio_key)
                print(json.dumps({"event": "passed", "job_id": job_id, "duration_seconds": result["duration_seconds"]}))
                return
            if result["status"] in {"failed", "cancelled"}:
                raise RuntimeError(f"Smoke-test job ended as {result['status']}")
            time.sleep(POLL_SECONDS)
        raise TimeoutError(f"Worker did not complete the smoke test within {TIMEOUT_SECONDS} seconds")
    finally:
        if audio_key:
            try:
                storage.delete(audio_key)
            except Exception as error:
                print(f"warning: could not delete smoke-test R2 object {audio_key}: {error}", file=sys.stderr)
        if work_id is not None:
            with psycopg.connect(settings.database_url) as connection:
                with connection.transaction():
                    with connection.cursor() as cursor:
                        # Only this script's work ID is targeted; tts_jobs does not cascade on episode deletion.
                        cursor.execute("DELETE FROM tts_jobs WHERE ep_id IN (SELECT ep_id FROM work_ep WHERE p_id = %s)", (work_id,))
                        cursor.execute("DELETE FROM works WHERE p_id = %s", (work_id,))
            print(json.dumps({"event": "cleanup_complete", "work_id": work_id}))


if __name__ == "__main__":
    main()
