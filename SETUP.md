# TTSCore Setup

Step-by-step guide for getting the Readji TTS worker running on a **new**
machine (a customer deployment, a second GPU box, or a fresh dev machine).
For day-to-day operation, configuration reference, and voice management once
the worker is already installed, see `README.md` instead — this file only
covers first-time setup.

## 1. Prerequisites

Check every one of these before starting. The worker fails fast if any is
missing, but it's faster to confirm up front.

| Requirement | Why | How to check |
|---|---|---|
| Windows | Only OS this worker has been built/tested on | — |
| NVIDIA GPU with a current driver, CUDA-capable | Model inference runs on GPU; there is no CPU fallback | `nvidia-smi` in a terminal — should print the GPU name and driver version |
| Python 3.10–3.12 | Pinned range in `pyproject.toml` | `py -3.11 --version` |
| ffmpeg on `PATH` | Used to encode the final MP3 | `ffmpeg -version` |
| Network access to the target PostgreSQL database | Worker claims jobs directly from `tts_jobs` (Phase A architecture) | confirm the DB host/port is reachable from this machine |
| A Cloudflare R2 API key scoped to the target media bucket | Worker uploads finished audio directly to R2 | get this from whoever manages that deployment's R2 account |

If this machine doesn't have a CUDA-capable GPU, stop here — nothing past
this point will work, and there is no non-GPU rendering path.

## 2. Get the code

```powershell
git clone <this repository>
cd TTSCore
```

## 3. Python environment and dependencies

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-lock.txt
python -m pip install -e .
```

`requirements-lock.txt` is a full freeze (every direct and transitive
dependency, ~150 packages, including `torch==2.11.0+cu128`) of exact
versions already proven to work — a real `pip freeze` from a working
install, not just the loose ranges in `pyproject.toml`. The second command
installs `readji_tts` itself from wherever this folder is, in editable mode
— deliberately not part of the frozen list.

No `requirements-lock.txt` present? Fall back to:
```powershell
python -m pip install torch==2.11.0 --index-url https://download.pytorch.org/whl/cu128
python -m pip install -e ".[dev]"
```
Same result, just resolved from `pyproject.toml`'s ranges instead of exact
pins.

`voxcpm==2.0.3` (the inference engine) installs automatically as part of this
step — it's a normal pinned dependency in `pyproject.toml`, not a separate
install.

## 4. Configure `.env`

```powershell
Copy-Item .env.example .env
```

Then edit `.env`. Every worker deployment owns its own `.env` — never copy a
developer's `.env` into a customer environment, and never commit a filled-in
`.env`.

**Must fill in:**

| Variable | Value |
|---|---|
| `DATABASE_URL` | Same PostgreSQL database the corresponding `apps/api` deployment uses |
| `R2_ENDPOINT_URL` | `https://<account_id>.r2.cloudflarestorage.com` |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | Scoped R2 key, read/write to the media bucket only |
| `R2_BUCKET_NAME` | Must be the same bucket `apps/api` reads from |
| `R2_PUBLIC_URL` | Same public media URL `apps/api` publishes (`R2_PUBLIC_URL` in its `.env`) |

**Everything else in `.env.example` already has a working default** — voice
paths, model ID, inference tuning, output format, poll interval, lease
timeout. Leave those alone on first setup; `README.md` explains what each one
does if a change is ever needed.

**Safety interlock:** if `DATABASE_URL` doesn't point at `localhost`/
`127.0.0.1`, the worker refuses to start unless `TTS_CONFIRM_REMOTE=1` is also
set in `.env`. This isn't a bug — it exists so a misconfigured `.env` can't
silently start claiming jobs against the wrong database. Only set it once
you've confirmed the `DATABASE_URL` is actually correct.

## 5. First run

```powershell
readji-tts-worker
```

This first launch downloads the `openbmb/VoxCPM2` model weights from Hugging
Face — a one-time download, cached outside the repo (in the Windows user
profile, not inside `TTSCore/`). It needs internet access and a few GB of
free disk space. Every run after this one starts from the local cache and
skips the download.

The worker refuses to process any job if it's missing the GPU, database
connection, R2 configuration, ffmpeg, or the three Basic-tier reference WAVs
under `assets/voices/Basic/` — it fails fast with a clear error instead of
publishing partial audio. If it starts and sits waiting for a job with no
error, setup is complete. Stop it with Ctrl+C once confirmed.

## 6. Day-to-day: use the GUI instead

After first setup, don't run `readji-tts-worker` from a terminal each time —
double-click **`Readji TTS Worker.bat`** in the repo root instead. It's a
thin launcher that starts the same worker as a background GUI/tray app using
the `.venv` created in step 3. It only works after steps 1–4 are done; run
against a missing `.venv` and it stops immediately with
`"virtual environment is missing"`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `.bat` opens and immediately closes with "virtual environment is missing" | Steps 2–3 weren't completed, or run from a copy of the repo in a different folder than where `.venv` was created |
| Worker exits immediately citing a non-local `DATABASE_URL` | Expected safety check — see step 4's interlock note |
| First run hangs for a long time with no output | Normal during the first-ever model download if the connection is slow; check Task Manager for network activity before assuming it's stuck |
| `RuntimeError: VoxCPM2 dependencies are not installed` | `pip install -e ".[dev]"` didn't complete — re-run step 3 inside the activated `.venv` |
| Worker starts but every job fails immediately | Check `R2_BUCKET_NAME`/`R2_PUBLIC_URL` match the `apps/api` deployment exactly — a mismatch here is the most common misconfiguration |

For anything about voice management, output audio format, retry/backoff
behavior, or the Pro-tier gate, see `README.md`.
