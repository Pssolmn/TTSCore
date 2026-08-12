from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
import logging
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import traceback

from botocore.exceptions import BotoCoreError, ClientError

from .audio import (
    concatenate_to_mp3,
    count_render_output_blocks,
    plan_tts_segments,
    split_text,
    timestamps_from_durations,
    write_silence_wav,
    write_wav,
)
from .config import Settings, VoiceProfile, load_basic_voice_profiles, load_settings
from .db import JobRepository
from .instance_lock import acquire_single_instance_lock
from .provider import VoxCpmNarrator
from .r2 import R2Storage
from .schemas import ClaimedJob


LOGGER = logging.getLogger("readji_tts")


@dataclass
class WorkerEvents:
    """Optional GUI hooks. Every field defaults to None, so a Worker built
    without `events=` (the headless `readji-tts-worker` entry point) behaves
    identically to before this existed."""

    on_activity: Callable[[], None] | None = None
    on_progress: Callable[[int, int, int, int | None], None] | None = None
    on_job_started: Callable[[ClaimedJob], None] | None = None
    on_job_completed: Callable[[int, float], None] | None = None
    on_job_failed: Callable[[int, str, str], None] | None = None


def classify_failure(error: Exception) -> tuple[str, str, bool]:
    """Return a stable admin-facing failure code, stage, and retry policy."""
    message = str(error).lower()
    if "lease was lost" in message or "cancelled" in message:
        return "LEASE_LOST_OR_CANCELLED", "lease", True
    if "no thai or english text remained" in message:
        return "NO_SUPPORTED_TEXT", "validation", False
    if "voice profile version mismatch" in message or "unknown tts voice slot" in message:
        return "VOICE_PROFILE_INVALID", "configuration", False
    if "reference wav" in message and "missing" in message:
        return "VOICE_REFERENCE_MISSING", "configuration", False
    if "dependencies are not installed" in message or "cuda is not available" in message:
        return "WORKER_CONFIGURATION", "configuration", False
    if "out of memory" in message or "cuda oom" in message:
        return "CUDA_OUT_OF_MEMORY", "synthesis", True
    if isinstance(error, subprocess.CalledProcessError):
        return "FFMPEG_FAILED", "concatenate", True
    if isinstance(error, (BotoCoreError, ClientError)):
        return "R2_OPERATION_FAILED", "upload", True
    return "UNEXPECTED_ERROR", "unknown", True


def configure_logging(log_file: Path) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
        force=True,
    )


class Worker:
    def __init__(self, settings: Settings, *, events: WorkerEvents | None = None) -> None:
        # First statement, before the DB/R2 connections are even opened: if
        # another copy already holds this GPU, fail immediately with nothing
        # else left to tear down.
        self._lock = acquire_single_instance_lock(settings.work_dir / "worker.lock")
        self.settings = settings
        self.events = events
        self.repository = JobRepository(
            settings.database_url,
            settings.worker_id,
            settings.lease_seconds,
            on_activity=events.on_activity if events else None,
            on_progress=events.on_progress if events else None,
        )
        self.storage = R2Storage(settings)
        self.voice_profiles = load_basic_voice_profiles(settings.voice_basic_path)
        self.narrator: VoxCpmNarrator | None = None
        self.running = True

    def close(self) -> None:
        self.repository.close()
        self._lock.release()

    def stop(self, *_: object) -> None:
        LOGGER.info("shutdown_requested")
        self.running = False

    def get_narrator(self) -> VoxCpmNarrator:
        if self.narrator is None:
            LOGGER.info("loading_model model=%s", self.settings.model_id)
            self.narrator = VoxCpmNarrator(self.settings)
            LOGGER.info("model_ready sample_rate=%s", self.narrator.sample_rate)
        return self.narrator

    def get_voice_profile(self, job: ClaimedJob) -> VoiceProfile:
        # Jobs produced before migration 039 are one narrator file.  They are
        # readable as the old_male slot; new jobs must match the active release.
        slot = "old_male" if job.voice_profile_version == "legacy" else job.voice_slot
        profile = self.voice_profiles.get(slot)
        if profile is None:
            raise RuntimeError(f"Unknown TTS voice slot on job {job.id}: {job.voice_slot}")
        if job.voice_profile_version != "legacy" and profile.version != job.voice_profile_version:
            raise RuntimeError(
                f"Voice profile version mismatch for job {job.id}: job={job.voice_profile_version}, machine={profile.version}"
            )
        return profile

    def run(self) -> None:
        self.repository.register_worker()
        while self.running:
            try:
                job = self.repository.claim_next()
                if job is None:
                    self.repository.touch_worker()
                    time.sleep(self.settings.poll_seconds)
                    continue
                self.process(job)
            except Exception:
                LOGGER.exception("worker_loop_error")
                time.sleep(self.settings.poll_seconds)

    def process(self, job: ClaimedJob) -> None:
        # This is deliberately before workspace creation, manifest writes,
        # model loading, and voice validation.  An oversized request must not
        # consume GPU time, local disk, or R2 operations before it is rejected.
        planned_output_blocks = count_render_output_blocks(
            job.blocks,
            self.settings.max_chunk_chars,
            stop_after=self.settings.max_output_blocks,
        )
        if planned_output_blocks > self.settings.max_output_blocks:
            error_message = (
                "TTS output block limit exceeded: this render would create at least "
                f"{planned_output_blocks} audio blocks; the maximum is "
                f"{self.settings.max_output_blocks}."
            )
            LOGGER.warning(
                "job_rejected_output_limit id=%s episode=%s planned_output_blocks_at_least=%s max_output_blocks=%s",
                job.id,
                job.ep_id,
                planned_output_blocks,
                self.settings.max_output_blocks,
            )
            self.repository.fail(
                job,
                error_message,
                code="OUTPUT_BLOCK_LIMIT_EXCEEDED",
                stage="validation",
                retryable=False,
            )
            return

        # Attempt-specific workspace avoids a hard-reboot orphan from blocking
        # the next lease reclaim for the same job.
        output_dir = self.settings.work_dir / f"job-{job.id}-attempt-{job.attempt_count}"
        audio_key: str | None = None
        uploaded = False
        current_block_index: int | None = None
        stage = "prepare"
        try:
            output_dir.mkdir(parents=True, exist_ok=False)
            self.repository.prepare_block_manifest(job)
            narrator = self.get_narrator()
            voice = self.get_voice_profile(job)
            LOGGER.info(
                "job_started id=%s episode=%s slot=%s version=%s attempt=%s work_title=%r ep_name=%r ep_no=%s",
                job.id, job.ep_id, job.voice_slot, job.voice_profile_version, job.attempt_count,
                job.work_title, job.ep_name, job.ep_no,
            )
            if self.events and self.events.on_job_started:
                self.events.on_job_started(job)
            wav_paths: list[Path] = []
            block_durations: list[tuple[str, float]] = []
            total_blocks = len(job.blocks)
            for index, block in enumerate(job.blocks):
                current_block_index = index
                current_block = index + 1
                if not self.repository.update_progress(
                    job.id,
                    total_blocks=total_blocks,
                    completed_blocks=index,
                    current_block=current_block,
                ):
                    raise RuntimeError("Job lease was lost or cancelled")
                self.repository.mark_block_started(job, index)
                duration = 0.0
                # Thai and English are intentionally retained. Other scripts are
                # removed before inference so mixed test/import data cannot push
                # unsupported text into the narrator model. Scene dividers and
                # ellipses become real silence WAVs instead of being spoken or
                # discarded, preserving the author's pacing in the final MP3.
                output_index = 0
                for segment in plan_tts_segments(block.tts_text or block.text, block.tts):
                    if segment.text:
                        chunks = split_text(segment.text, self.settings.max_chunk_chars)
                        for chunk in chunks:
                            # A cancellation or shutdown takes effect before the
                            # next chunk; a CUDA inference call cannot be
                            # interrupted safely.
                            if not self.running or not self.repository.update_progress(
                                job.id,
                                total_blocks=total_blocks,
                                completed_blocks=index,
                                current_block=current_block,
                            ):
                                raise RuntimeError("Job lease was lost, cancelled, or worker is shutting down")
                            wav_path = output_dir / f"{index:05d}-{output_index:03d}.wav"
                            stage = "synthesis"
                            duration += write_wav(wav_path, narrator.synthesize(chunk, voice.reference_wav_path), narrator.sample_rate)
                            wav_paths.append(wav_path)
                            output_index += 1
                    elif segment.silence_seconds > 0:
                        if not self.running or not self.repository.update_progress(
                            job.id,
                            total_blocks=total_blocks,
                            completed_blocks=index,
                            current_block=current_block,
                        ):
                            raise RuntimeError("Job lease was lost, cancelled, or worker is shutting down")
                        wav_path = output_dir / f"{index:05d}-{output_index:03d}.wav"
                        stage = "synthesis"
                        duration += write_silence_wav(wav_path, segment.silence_seconds, narrator.sample_rate)
                        wav_paths.append(wav_path)
                        output_index += 1
                block_durations.append((block.id, duration))
                self.repository.complete_block(job, index, duration)
                if not self.repository.update_progress(
                    job.id,
                    total_blocks=total_blocks,
                    completed_blocks=current_block,
                    current_block=current_block + 1 if current_block < total_blocks else None,
                ):
                    raise RuntimeError("Job lease was lost or cancelled")
                LOGGER.info("job_progress id=%s completed_blocks=%s total_blocks=%s", job.id, current_block, total_blocks)
            current_block_index = None
            timestamps = timestamps_from_durations(block_durations)
            if not wav_paths:
                raise RuntimeError("No Thai or English text remained after TTS language filtering")
            output_mp3 = output_dir / "full.mp3"
            stage = "concatenate"
            duration_seconds = concatenate_to_mp3(wav_paths, output_mp3, ffmpeg_path=self.settings.ffmpeg_path)
            if duration_seconds <= 0:
                raise RuntimeError("Concatenated episode audio has zero duration")
            stage = "upload"
            audio_key = self.storage.audio_key(job.ep_id, job.voice_slot, job.voice_profile_version, job.id, job.attempt_count)
            self.storage.upload_mp3(audio_key, output_mp3)
            uploaded = True
            stage = "commit"
            committed = self.repository.complete(
                job,
                audio_key=audio_key,
                audio_url=self.storage.public_object_url(audio_key),
                duration_seconds=duration_seconds,
                timestamps=timestamps,
            )
            if not committed:
                try:
                    self.storage.delete(audio_key)
                except Exception as cleanup_error:
                    self.repository.record_cleanup_failure(job, f"{type(cleanup_error).__name__}: {cleanup_error}")
                uploaded = False
                LOGGER.info("job_discarded_stale_or_cancelled id=%s", job.id)
                return
            LOGGER.info("job_completed id=%s episode=%s slot=%s duration=%.3f", job.id, job.ep_id, job.voice_slot, duration_seconds)
            if self.events and self.events.on_job_completed:
                self.events.on_job_completed(job.id, duration_seconds)
        except Exception as error:
            if uploaded and audio_key is not None:
                try:
                    self.storage.delete(audio_key)
                except Exception as cleanup_error:
                    with suppress(Exception):
                        self.repository.record_cleanup_failure(job, f"{type(cleanup_error).__name__}: {cleanup_error}")
            error_message = f"{type(error).__name__}: {error}"
            code, classified_stage, retryable = classify_failure(error)
            # Preserve a caller-supplied stage when it is more specific than
            # the generic classifier (for example R2 upload vs final commit).
            failure_stage = stage if stage not in {"prepare", "synthesis"} else classified_stage
            if code == "CUDA_OUT_OF_MEMORY" and self.narrator is not None:
                self.narrator.release_cuda_cache()
            LOGGER.error("job_failed id=%s error=%s", job.id, error_message)
            LOGGER.debug("job_traceback id=%s\n%s", job.id, traceback.format_exc())
            if self.events and self.events.on_job_failed:
                self.events.on_job_failed(job.id, error_message, code)
            with suppress(Exception):
                self.repository.fail(
                    job,
                    error_message,
                    code=code,
                    stage=failure_stage,
                    retryable=retryable,
                    current_block=current_block_index,
                )
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)


def main() -> None:
    settings = load_settings()
    configure_logging(settings.work_dir / "worker.log")
    current_worker: Worker | None = None
    shutdown_requested = False

    def stop(*_: object) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True
        if current_worker is not None:
            current_worker.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while not shutdown_requested:
        try:
            current_worker = Worker(settings)
            current_worker.run()
            return
        except Exception:
            LOGGER.exception("worker_startup_error_retrying")
            if not shutdown_requested:
                time.sleep(15)
        finally:
            if current_worker is not None:
                current_worker.close()
                current_worker = None


if __name__ == "__main__":
    main()
