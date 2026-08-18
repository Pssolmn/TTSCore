# Readji TTS Worker

This is the production Python worker for Readji's Tier 0 novel narration. It claims one job at a time from `tts_jobs`, synthesizes every `NovelBlock` with VoxCPM2, concatenates the audio with ffmpeg, uploads an immutable MP3 to Cloudflare R2, and commits block timestamps only if the episode has not changed.

Manual Tier 1/Pro wiring is also present: one `pro` job can use a frozen
per-slot assignment snapshot and switch the reference WAV per block. It is
**disabled by default** with `TTS_PRO_RENDER_ENABLED=false`. Do not enable it
until Pro reference folders have been checked and a small end-to-end test has
been explicitly approved.

## Prerequisites

- Windows with NVIDIA driver and CUDA-capable GPU (the current RTX 4060 runs one sequential worker)
- Python 3.10–3.12, ffmpeg available in `PATH`
- PostgreSQL used by the corresponding Novel Platform deployment
- Cloudflare R2 credentials with object read/write permission for that deployment's media bucket

## Install

```powershell
cd "C:\Users\SEESO\Documents\Web dev\TTSCore"
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install torch --index-url https://download.pytorch.org/whl/cu128
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Every worker deployment owns its own `.env` (or `TTS_ENV_FILE`). It never reads
credentials from a sibling Novel Platform checkout. This keeps a local test,
your own instance, and a customer's instance fully separate.

## Configuration and R2 connection

`.env.example` is the complete safe template for the worker. It contains only
placeholders and may be committed; `.env` contains real credentials and is
ignored. The worker needs these connections before it can claim a job:

- `DATABASE_URL` — the PostgreSQL database used by the Novel Platform API
- `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
  `R2_BUCKET_NAME`, and `R2_PUBLIC_URL` — Cloudflare R2 for final audio

Create the R2 key with read/write access to the customer's bucket only. The
worker and its corresponding API must use the same customer R2 values so the
API can publish the audio the worker uploads. Do not place R2 keys, database
passwords, JWT secrets, or payment secrets in Web/Admin
`NEXT_PUBLIC_*` variables: those values are delivered to every browser.

For a dedicated production worker, copy `.env.example` to a private file such
as `.env.production`, set `TTS_ENV_FILE=.env.production` for its service, and
provide the real values through the server's secret manager. The worker
refuses a non-local `DATABASE_URL` unless `TTS_CONFIRM_REMOTE=1` is also set;
this is a deliberate confirmation that it may claim production jobs. Keep
`TTS_PRO_RENDER_ENABLED=false` until the Pro reference folders and a small
end-to-end render have been approved.

## Reader voice slots (Basic tier)

Basic-tier chapters are always rendered with a single narrator voice, one of three fixed reader-facing slots: `old_male` (ชายแก่), `young_male` (หนุ่มน้อย), and `female` (คุณผู้หญิง). The source WAV for each slot is a real file on disk at `assets/voices/Basic/Basic_<slot>.wav` -- there is no config file to edit. To replace a voice, place the legally usable WAV directly at that path before queuing new renders. The profile version is hardcoded to `v1` in `load_basic_voice_profiles()`; every render has a unique job ID in its R2 key, so it is safe to replace a source file without changing reader slot names or releasing the frontend.

## Per-voice timing

Open **ตั้งค่า** in the desktop worker, select a WAV in **Voice render settings**, and set its optional silence before the episode starts and between spoken blocks. The tray scans every `.wav` below `assets/voices/`, including newly copied Pro-category folders; a new file defaults to zero added silence until it is configured. Values are stored by portable relative path in `assets/voices/voice-render-settings.json`, take effect on the next job the worker claims, and are included in the final timestamps. The starting preset for `Basic/Basic_female.wav` is 0.18 seconds before the episode and 0.12 seconds between ordinary spoken blocks.

Changing a timing value cannot alter an MP3 already published to R2. Queue a new render for the episode/voice after saving if the existing audio should use the new pacing.

The current files are the real candidate voices already reviewed on this machine: `assets/voices/Basic/Basic_old_male.wav` (ชายแก่), `assets/voices/Basic/Basic_young_male.wav` (หนุ่มน้อย), and `assets/voices/Basic/Basic_female.wav` (คุณผู้หญิง).

## Published audio format and size

Every completed job publishes a **mono MP3 at 32 kHz / 32 kbps** by default.
This is roughly one quarter of the former 128 kbps output size while remaining
appropriate for spoken narration. MP3 is compressed audio and has no fixed
"16-bit" field; the worker does use a 16-bit PCM WAV only as an internal
ffmpeg intermediate, then deletes it before the R2 upload.

Set these only when a deployment needs a different trade-off, then restart the
worker. They affect newly rendered jobs only; an MP3 already on R2 must be
rendered again to change its format.

```env
TTS_OUTPUT_SAMPLE_RATE=32000
TTS_OUTPUT_MP3_BITRATE_KBPS=32
```

For noticeably higher fidelity, a practical next step is `48000` Hz and
`48` kbps; increasing bitrate increases file size almost linearly.

### Writer preview for Basic voices

The public writer preview is a separate, generated WAV bundled with the web
app at `Novel Platform/apps/web/public/audio/tts-samples/basic/<slot>.wav`.
It is deliberately not uploaded to R2 and it never replaces the source WAV in
`assets/voices/Basic/`. Generate all three preview files after approving a
source voice (or after replacing one) with:

```powershell
readji-tts-basic-preview
```

That command uses each current `Basic_<slot>.wav` as VoxCPM's reference voice
and speaks one fixed demonstration sentence. Commit/deploy the generated web
files with the web app; moving that app carries the previews with it.

## Novel text handling

Before inference, the worker keeps Thai, English, digits, and narration punctuation only; other scripts become spaces so words never join together. A line made only of `*` characters becomes a 1.25-second scene-break silence. Ellipses (`...` and `…`) become 0.55-second silence between the surrounding phrases. Both pauses are written as WAVs and included in block timestamps, so the reader highlighter and click-to-seek remain in sync with the final MP3.

## Create voice candidates

```powershell
readji-tts-voice-design
```

The command writes Thai narration candidates into `assets/voice-candidates`. Listen to them and copy the approved, legally usable file to the matching slot at `assets/voices/Basic/Basic_<slot>.wav`. `TTS_MASTER_VOICE_PATH` remains only for legacy voice-design tooling; normal reader jobs use the Basic slot files.

## Run

```powershell
readji-tts-worker
```

The first run downloads `openbmb/VoxCPM2`. A worker does not process any job without the three Basic reference WAVs, GPU, database connection, R2 configuration, or ffmpeg; it fails fast instead of publishing partial audio. `TTS_MASTER_VOICE_PATH` is required only by legacy voice-design tooling.

## Operating guarantees

- Job claiming uses `FOR UPDATE SKIP LOCKED`, a worker ID, and an expiring lease.
- Failed jobs are classified by code/stage, recorded in the append-only `tts_job_events` log, and retry with backoff (15s, 30s, 60s) up to `max_attempts`; then become `failed`. Expired final leases are failed instead of remaining stuck in `processing`.
- Admin can cancel a queued/processing request safely or retry only its failed/cancelled voice jobs after a configuration problem is fixed. Every manual action is audited and recorded as an event.
- `tts_job_blocks` stores the durable block ID, source-text hash, render status, duration, and final timestamp for every attempt. This is the backend foundation for a later repair-one-chunk editor; it does not yet expose a new editor screen.
- A hash of canonical content is checked immediately before timestamps and status are committed. Editing an episode while it is rendering never overwrites the edited revision.
- Before the GPU is loaded, the worker counts the actual temporary audio blocks it would emit (spoken chunks plus intentional silences). A job that would exceed the hard 500-block ceiling is failed once with `OUTPUT_BLOCK_LIMIT_EXCEEDED`; it creates no workspace files, does not call the model, and is never retried automatically. `TTS_MAX_OUTPUT_BLOCKS` may lower this ceiling for maintenance, but cannot raise it above 500.
- Each output key is unique to the episode, voice slot, profile version, job, and attempt: `episode-audio/{episode-id}/{slot}/{version}/job-{job-id}/attempt-{attempt}/full.mp3`, so an immutable CDN cache can never serve an older retry as a new revision.
- Rendering is deliberately sequential per GPU. Run another worker only on a separate GPU-equipped machine.

## Current local installation

The local GPU machine is already configured with Python 3.11, CUDA 12.8, VoxCPM2 weights, the neutral master narrator, PostgreSQL/R2 connectivity, and a `ReadjiTtsWorker` Windows Scheduled Task. Its durable log is `runtime/worker.log`. Do not run a second copy of the worker on this same GPU.

Before a public deployment, rotate the existing development database/R2 secrets and supply those new values through a dedicated worker environment file when the worker is moved off this machine.

## End-to-end smoke test

`python scripts/integration-smoke-test.py` creates a uniquely named private test work and two short Thai blocks, waits for the already-running worker to render/upload/commit them, validates the R2 object and timestamps, then deletes the test work, job, and R2 object. It never targets an existing work.
