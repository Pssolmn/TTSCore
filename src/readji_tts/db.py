from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import hashlib
import json
import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .schemas import ClaimedJob, NovelBlock, source_hash, with_timestamps, BlockTimestamp

LOGGER = logging.getLogger("readji_tts")


class JobRepository:
    def __init__(
        self,
        database_url: str,
        worker_id: str,
        lease_seconds: int,
        *,
        on_activity: Callable[[], None] | None = None,
        on_progress: Callable[[int, int, int, int | None], None] | None = None,
    ) -> None:
        self.connection = psycopg.connect(database_url, row_factory=dict_row)
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        # GUI hooks only. Both default to None so the headless worker entry
        # point is byte-for-byte unaffected when it constructs a repository.
        self._on_activity = on_activity
        self._on_progress = on_progress

    def close(self) -> None:
        self.connection.close()

    def register_worker(self) -> None:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO tts_worker_heartbeats (worker_id, started_at, last_seen_at, current_job_id)
                    VALUES (%s, now(), now(), NULL)
                    ON CONFLICT (worker_id) DO UPDATE
                    SET started_at = now(), last_seen_at = now(), current_job_id = NULL
                    """,
                    (self.worker_id,),
                )

    def touch_worker(self, current_job_id: int | None = None) -> None:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                self._touch_worker(cursor, current_job_id)

    def _touch_worker(self, cursor: Any, current_job_id: int | None) -> None:
        cursor.execute(
            """
            INSERT INTO tts_worker_heartbeats (worker_id, started_at, last_seen_at, current_job_id)
            VALUES (%s, now(), now(), %s)
            ON CONFLICT (worker_id) DO UPDATE
            SET last_seen_at = now(), current_job_id = EXCLUDED.current_job_id
            """,
            (self.worker_id, current_job_id),
        )
        # Fires on every poll cycle whether idle or busy, so this is the GUI's
        # connection-status signal: a real, frequent, truthful "we just talked
        # to Postgres successfully" event with no second/racing connection.
        if self._on_activity:
            self._on_activity()

    def _record_event(
        self,
        cursor: Any,
        *,
        job_id: int,
        request_id: int,
        event_type: str,
        severity: str = "info",
        code: str | None = None,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO tts_job_events (job_id, request_id, event_type, severity, code, message, context)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (job_id, request_id, event_type, severity, code, message[:2000], Jsonb(context or {})),
        )

    def _refresh_request_status(self, cursor: Any, request_id: int) -> None:
        cursor.execute("SELECT id FROM tts_requests WHERE id = %s FOR UPDATE", (request_id,))
        if cursor.fetchone() is None:
            return
        cursor.execute(
            """
            SELECT
              count(*)::int AS total,
              count(*) FILTER (WHERE status = 'done')::int AS done,
              count(*) FILTER (WHERE status = 'failed')::int AS failed,
              count(*) FILTER (WHERE status = 'cancelled')::int AS cancelled,
              count(*) FILTER (WHERE status = 'processing')::int AS processing,
              count(*) FILTER (WHERE status = 'pending')::int AS pending
            FROM tts_jobs
            WHERE request_id = %s
            """,
            (request_id,),
        )
        counts = cursor.fetchone()
        if counts is None or int(counts["total"]) == 0:
            return
        total = int(counts["total"])
        done = int(counts["done"])
        if int(counts["processing"]) > 0:
            status = "processing"
        elif int(counts["pending"]) > 0:
            status = "queued"
        elif done == total:
            status = "completed"
        elif int(counts["failed"]) > 0:
            status = "failed"
        elif int(counts["cancelled"]) > 0:
            status = "cancelled"
        else:
            status = "queued"
        cursor.execute(
            """
            UPDATE tts_requests
            SET status = %s,
                completed_at = CASE
                  WHEN %s IN ('completed', 'failed', 'cancelled') THEN COALESCE(completed_at, now())
                  ELSE completed_at
                END,
                updated_at = now()
            WHERE id = %s
            """,
            (status, status, request_id),
        )

    def claim_next(self) -> ClaimedJob | None:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                # A worker can disappear while CUDA is running.  Do not allow
                # an expired final attempt to remain "processing" forever.
                cursor.execute(
                    """
                    UPDATE tts_jobs
                    SET status = 'failed', failure_code = 'LEASE_EXPIRED', failure_stage = 'worker',
                        retryable = TRUE, error_message = 'Worker lease expired before the render completed',
                        last_error_at = now(), lease_expires_at = NULL, updated_at = now()
                    WHERE status = 'processing'
                      AND lease_expires_at < now()
                      AND attempt_count >= max_attempts
                    RETURNING id, request_id
                    """
                )
                for expired in cursor.fetchall():
                    self._record_event(
                        cursor,
                        job_id=int(expired["id"]),
                        request_id=int(expired["request_id"]),
                        event_type="failed",
                        severity="error",
                        code="LEASE_EXPIRED",
                        message="Worker lease expired after the final permitted attempt",
                    )
                    self._refresh_request_status(cursor, int(expired["request_id"]))
                cursor.execute(
                    """
                    WITH next_job AS (
                      SELECT id, status AS previous_status
                      FROM tts_jobs
                      WHERE request_id IS NOT NULL
                        AND ((status = 'pending' AND available_at <= now())
                         OR (status = 'processing' AND lease_expires_at < now())
                        )
                        AND attempt_count < max_attempts
                      ORDER BY priority DESC, requested_at
                      FOR UPDATE SKIP LOCKED
                      LIMIT 1
                    )
                    UPDATE tts_jobs AS job
                    SET status = 'processing',
                        worker_id = %(worker_id)s,
                        attempt_count = attempt_count + 1,
                        lease_expires_at = now() + (%(lease_seconds)s * interval '1 second'),
                        started_at = COALESCE(started_at, now()),
                        updated_at = now(),
                        error_message = NULL
                    FROM next_job
                    WHERE job.id = next_job.id
                    RETURNING job.id, job.ep_id, job.request_id, job.voice_slot, job.voice_profile_version,
                              job.source_hash, job.voice_assignments, job.attempt_count, job.max_attempts, next_job.previous_status
                    """,
                    {"worker_id": self.worker_id, "lease_seconds": self.lease_seconds},
                )
                job = cursor.fetchone()
                if job is None:
                    return None
                previous_status = str(job["previous_status"])
                self._record_event(
                    cursor,
                    job_id=int(job["id"]),
                    request_id=int(job["request_id"]),
                    event_type="lease_reclaimed" if previous_status == "processing" else "claimed",
                    severity="warning" if previous_status == "processing" else "info",
                    code="LEASE_EXPIRED" if previous_status == "processing" else None,
                    message="Reclaimed an expired worker lease" if previous_status == "processing" else "Job claimed by worker",
                    context={"worker_id": self.worker_id, "attempt": int(job["attempt_count"])},
                )
                cursor.execute(
                    "UPDATE tts_requests SET status = 'processing', started_at = COALESCE(started_at, now()), updated_at = now() WHERE id = %s",
                    (job["request_id"],),
                )
                self._touch_worker(cursor, int(job["id"]))
                # Display-only enrichment (work title / episode name+number)
                # for the GUI's status line and log. LEFT JOIN, not INNER: if
                # `works` were ever missing for a p_id, an inner join would
                # silently drop the work_ep row too, turning a missing
                # *enrichment* field into a false "episode disappeared"
                # failure below. The nested transaction becomes a SAVEPOINT
                # in psycopg3, so a genuine SQL error here (e.g. a future
                # column rename) only unwinds this attempt -- the outer
                # transaction, and the plain fallback query, stay usable.
                try:
                    with self.connection.transaction():
                        cursor.execute(
                            """
                            SELECT we.ep_content, we.ep_name, we.ep_no, w.title AS work_title
                            FROM work_ep we
                            LEFT JOIN works w ON w.p_id = we.p_id
                            WHERE we.ep_id = %s
                            FOR SHARE
                            """,
                            (job["ep_id"],),
                        )
                        episode = cursor.fetchone()
                except Exception:
                    LOGGER.warning("episode_metadata_join_failed ep_id=%s", job["ep_id"])
                    cursor.execute("SELECT ep_content FROM work_ep WHERE ep_id = %s FOR SHARE", (job["ep_id"],))
                    episode = cursor.fetchone()
                if episode is None:
                    raise RuntimeError(f"Episode {job['ep_id']} disappeared after job {job['id']} was claimed")
                raw_blocks = episode["ep_content"] or []
                if not isinstance(raw_blocks, list):
                    raise RuntimeError(f"Episode {job['ep_id']} has an invalid ep_content value")
                ep_no = episode.get("ep_no")
                return ClaimedJob(
                    id=int(job["id"]),
                    ep_id=int(job["ep_id"]),
                    request_id=int(job["request_id"]),
                    voice_slot=str(job["voice_slot"]),
                    voice_profile_version=str(job["voice_profile_version"]),
                    source_hash=str(job["source_hash"]),
                    attempt_count=int(job["attempt_count"]),
                    max_attempts=int(job["max_attempts"]),
                    blocks=[NovelBlock.from_db(item) for item in raw_blocks],
                    voice_assignments=job.get("voice_assignments"),
                    work_title=episode.get("work_title"),
                    ep_name=episode.get("ep_name"),
                    ep_no=int(ep_no) if ep_no is not None else None,
                )

    def prepare_block_manifest(self, job: ClaimedJob) -> None:
        """Reset one job's block manifest for its current render attempt.

        The rows are durable metadata, not temporary files. A future repair
        endpoint can therefore safely target a block ID/source hash without
        guessing from MP3 byte offsets.
        """
        rows = [
            (
                job.id,
                block.id,
                index,
                hashlib.sha256((block.tts_text or block.text).encode("utf-8")).hexdigest(),
            )
            for index, block in enumerate(job.blocks)
        ]
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO tts_job_blocks (job_id, block_id, block_index, source_text_hash, status)
                    VALUES (%s, %s, %s, %s, 'pending')
                    ON CONFLICT (job_id, block_id) DO UPDATE
                    SET block_index = EXCLUDED.block_index,
                        source_text_hash = EXCLUDED.source_text_hash,
                        status = 'pending', duration_seconds = NULL, start_seconds = NULL, end_seconds = NULL,
                        error_code = NULL, error_message = NULL, started_at = NULL, completed_at = NULL,
                        updated_at = now()
                    """,
                    rows,
                )

    def mark_block_started(self, job: ClaimedJob, block_index: int) -> None:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE tts_job_blocks
                    SET status = 'processing', started_at = now(), error_code = NULL, error_message = NULL, updated_at = now()
                    WHERE job_id = %s AND block_index = %s
                    """,
                    (job.id, block_index),
                )

    def complete_block(self, job: ClaimedJob, block_index: int, duration_seconds: float) -> None:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE tts_job_blocks
                    SET status = %s, duration_seconds = %s, completed_at = now(), updated_at = now()
                    WHERE job_id = %s AND block_index = %s
                    """,
                    ("done" if duration_seconds > 0 else "skipped", duration_seconds, job.id, block_index),
                )

    def mark_block_failed(self, job: ClaimedJob, block_index: int | None, code: str, error: str) -> None:
        if block_index is None:
            return
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE tts_job_blocks
                    SET status = 'failed', error_code = %s, error_message = %s, updated_at = now()
                    WHERE job_id = %s AND block_index = %s
                    """,
                    (code, error[-2000:], job.id, block_index),
                )

    def update_progress(
        self,
        job_id: int,
        *,
        total_blocks: int,
        completed_blocks: int,
        current_block: int | None,
    ) -> bool:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE tts_jobs
                    SET lease_expires_at = now() + (%s * interval '1 second'),
                        total_blocks = %s,
                        completed_blocks = %s,
                        current_block = %s,
                        progress_updated_at = now(),
                        updated_at = now()
                    WHERE id = %s AND status = 'processing' AND worker_id = %s
                    """,
                    (self.lease_seconds, total_blocks, completed_blocks, current_block, job_id, self.worker_id),
                )
                updated = cursor.rowcount == 1
                self._touch_worker(cursor, job_id)
                # Gated on `updated`: a doomed final call right before a
                # lease-loss failure must not tick the GUI's progress bar
                # with numbers that never actually got committed.
                if updated and self._on_progress:
                    self._on_progress(job_id, total_blocks, completed_blocks, current_block)
                return updated

    def complete(
        self,
        job: ClaimedJob,
        audio_key: str,
        audio_url: str,
        duration_seconds: float,
        timestamps: list[BlockTimestamp],
    ) -> bool:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT ep_content
                    FROM work_ep
                    WHERE ep_id = %s
                    FOR UPDATE
                    """,
                    (job.ep_id,),
                )
                episode = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT status, worker_id, source_hash
                    FROM tts_jobs
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (job.id,),
                )
                current_job = cursor.fetchone()
                if episode is None or current_job is None:
                    return False
                if current_job["status"] != "processing" or current_job["worker_id"] != self.worker_id:
                    return False
                raw_blocks = episode["ep_content"] or []
                if not isinstance(raw_blocks, list) or source_hash(raw_blocks) != job.source_hash:
                    cursor.execute(
                        """
                        UPDATE tts_jobs
                        SET status = 'cancelled', error_message = 'CONTENT_CHANGED', failure_code = 'CONTENT_CHANGED',
                            failure_stage = 'validation', retryable = FALSE, cancelled_at = now(),
                            lease_expires_at = NULL, updated_at = now()
                        WHERE id = %s
                        """,
                        (job.id,),
                    )
                    cursor.execute(
                        """
                        UPDATE tts_job_blocks
                        SET status = 'cancelled', error_code = 'CONTENT_CHANGED', error_message = 'Content changed before publish', updated_at = now()
                        WHERE job_id = %s AND status IN ('pending', 'processing')
                        """,
                        (job.id,),
                    )
                    self._record_event(
                        cursor,
                        job_id=job.id,
                        request_id=job.request_id,
                        event_type="cancelled",
                        severity="warning",
                        code="CONTENT_CHANGED",
                        message="Render output was discarded because the episode content changed",
                    )
                    self._refresh_request_status(cursor, job.request_id)
                    self._touch_worker(cursor, None)
                    return False
                updated_blocks = with_timestamps(raw_blocks, timestamps)
                cursor.execute(
                    "UPDATE work_ep SET ep_content = %s WHERE ep_id = %s",
                    (Jsonb(updated_blocks), job.ep_id),
                )
                cursor.execute(
                    """
                    UPDATE tts_jobs
                    SET status = 'done', audio_key = %s, audio_url = %s, duration_seconds = %s,
                        lease_expires_at = NULL, completed_blocks = total_blocks, current_block = NULL,
                        progress_updated_at = now(), completed_at = now(), updated_at = now(), error_message = NULL
                    WHERE id = %s
                    """,
                    (audio_key, audio_url, duration_seconds, job.id),
                )
                for timestamp in timestamps:
                    cursor.execute(
                        """
                        UPDATE tts_job_blocks
                        SET start_seconds = %s, end_seconds = %s,
                            status = CASE WHEN status = 'skipped' THEN 'skipped' ELSE 'done' END,
                            completed_at = COALESCE(completed_at, now()), updated_at = now()
                        WHERE job_id = %s AND block_id = %s
                        """,
                        (timestamp.start, timestamp.end, job.id, timestamp.block_id),
                    )
                self._record_event(
                    cursor,
                    job_id=job.id,
                    request_id=job.request_id,
                    event_type="completed",
                    message="Episode narration rendered and uploaded",
                    context={"duration_seconds": round(duration_seconds, 3), "voice_slot": job.voice_slot},
                )
                self._refresh_request_status(cursor, job.request_id)
                self._touch_worker(cursor, None)
                return True

    def fail(
        self,
        job: ClaimedJob,
        error: str,
        *,
        code: str = "UNEXPECTED_ERROR",
        stage: str = "unknown",
        retryable: bool = True,
        current_block: int | None = None,
    ) -> None:
        safe_error = error[-2000:]
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status, worker_id, attempt_count, max_attempts FROM tts_jobs WHERE id = %s FOR UPDATE",
                    (job.id,),
                )
                current = cursor.fetchone()
                if current is None or current["status"] != "processing" or current["worker_id"] != self.worker_id:
                    return
                should_retry = retryable and int(current["attempt_count"]) < int(current["max_attempts"])
                if not should_retry:
                    cursor.execute(
                        """
                        UPDATE tts_jobs
                        SET status = 'failed', error_message = %s, failure_code = %s, failure_stage = %s,
                            retryable = %s, last_error_at = now(), lease_expires_at = NULL, updated_at = now()
                        WHERE id = %s
                        """,
                        (safe_error, code, stage, retryable, job.id),
                    )
                    if current_block is not None:
                        cursor.execute(
                            """
                            UPDATE tts_job_blocks
                            SET status = 'failed', error_code = %s, error_message = %s, updated_at = now()
                            WHERE job_id = %s AND block_index = %s
                            """,
                            (code, safe_error, job.id, current_block),
                        )
                    self._record_event(
                        cursor,
                        job_id=job.id,
                        request_id=job.request_id,
                        event_type="failed",
                        severity="error",
                        code=code,
                        message=safe_error,
                        context={"stage": stage, "attempt": int(current["attempt_count"]), "retryable": retryable},
                    )
                    self._refresh_request_status(cursor, job.request_id)
                    self._touch_worker(cursor, None)
                    return
                delay_seconds = min(300, 15 * (2 ** max(0, int(current["attempt_count"]) - 1)))
                cursor.execute(
                    """
                    UPDATE tts_jobs
                    SET status = 'pending', error_message = %s, failure_code = %s, failure_stage = %s,
                        retryable = TRUE, last_error_at = now(), lease_expires_at = NULL,
                        available_at = now() + (%s * interval '1 second'), updated_at = now()
                    WHERE id = %s
                    """,
                    (safe_error, code, stage, delay_seconds, job.id),
                )
                if current_block is not None:
                    cursor.execute(
                        """
                        UPDATE tts_job_blocks
                        SET status = 'failed', error_code = %s, error_message = %s, updated_at = now()
                        WHERE job_id = %s AND block_index = %s
                        """,
                        (code, safe_error, job.id, current_block),
                    )
                self._record_event(
                    cursor,
                    job_id=job.id,
                    request_id=job.request_id,
                    event_type="retry_scheduled",
                    severity="warning",
                    code=code,
                    message=f"{safe_error} (retry in {delay_seconds}s)",
                    context={"stage": stage, "attempt": int(current["attempt_count"]), "retry_in_seconds": delay_seconds},
                )
                self._refresh_request_status(cursor, job.request_id)
                self._touch_worker(cursor, None)

    def record_cleanup_failure(self, job: ClaimedJob, error: str) -> None:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                self._record_event(
                    cursor,
                    job_id=job.id,
                    request_id=job.request_id,
                    event_type="cleanup_failed",
                    severity="warning",
                    code="R2_CLEANUP_FAILED",
                    message=error,
                )
