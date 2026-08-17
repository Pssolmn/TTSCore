from __future__ import annotations

from datetime import datetime
import logging
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .config import Settings, describe_database_target, load_basic_voice_profiles, load_settings, resolve_env_file
from .instance_lock import AlreadyRunningError
from .scheduled_task import get_boot_task_state, set_boot_task_state
from .schemas import ClaimedJob
from .voice_render_settings import DEFAULT_TIMING, VoiceRenderSettingsStore, VoiceRenderTiming, scan_voice_reference_files
from .worker import LOGGER as WORKER_LOGGER, Worker, WorkerEvents, configure_logging

LOG_PANEL_MAX_LINES = 2000
POLL_MS = 150
# ~4x the default TTS_POLL_SECONDS (5s): long enough that a normal idle poll
# gap never trips it, short enough to notice a genuinely wedged connection.
STALE_ACTIVITY_SECONDS = 20
# Generous upper bound for "wait for the current chunk to finish" on close --
# a single VoxCPM2 chunk plus its DB write should never approach this, but a
# hard cap means the window always closes eventually even if something hangs.
SHUTDOWN_TIMEOUT_SECONDS = 60

# Tk's own default (Tahoma/MS Sans Serif on Windows) renders Thai glyphs
# cramped and low-fidelity. Try the common TH Sarabun variants in rough
# popularity order; silently keep Tk's default if none are installed.
PREFERRED_THAI_FONTS = ("TH Sarabun New", "TH SarabunPSK", "TH Sarabun")
THAI_FONT_SIZE = 13


def _configure_thai_font(root: tk.Tk) -> None:
    available = set(tkfont.families(root))
    family = next((name for name in PREFERRED_THAI_FONTS if name in available), None)
    if family is None:
        return
    # Every stock widget used here (Label, Button, LabelFrame, and Text via
    # ScrolledText) draws from one of these named fonts unless overridden
    # per-widget, so reconfiguring them covers the whole window in one go.
    # TkFixedFont matters specifically because tkinter.Text (ScrolledText's
    # base) defaults to it, not TkTextFont.
    for named_font in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont", "TkCaptionFont", "TkFixedFont"):
        try:
            tkfont.nametofont(named_font).configure(family=family, size=THAI_FONT_SIZE)
        except tk.TclError:
            pass


class QueueLogHandler(logging.Handler):
    """Feeds every existing LOGGER.info/warning/error call into the GUI's
    log panel, without touching any of the call sites that already exist."""

    def __init__(self, event_queue: "queue.Queue") -> None:
        super().__init__()
        self._queue = event_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        self._queue.put(("log", record.levelno, message))


def _redact_last4(value: str) -> str:
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


def _job_label(job: ClaimedJob) -> str:
    if job.work_title:
        episode = f"ตอนที่ {job.ep_no}: {job.ep_name}" if job.ep_no is not None else (job.ep_name or "")
        return f'"{job.work_title}" — {episode}'.rstrip(" —")
    return f"episode {job.ep_id}"


class ReadjiTtsGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Readji TTS Worker")
        # Sized for the TH Sarabun New default (13pt): a plain Tk default font
        # would fit in less, but this font's taller line-height pushed the
        # button row off the bottom of the original 760x540 in testing.
        self.root.geometry("780x640")
        self.root.minsize(600, 480)

        self.event_queue: "queue.Queue" = queue.Queue()
        self.worker: Worker | None = None
        self.settings: Settings | None = None
        self.voice_render_settings: VoiceRenderSettingsStore | None = None
        self.worker_thread: threading.Thread | None = None
        self._closing = False
        self._shutdown_deadline = 0.0
        self._last_activity_at: float | None = None
        self._connection_state = "connecting"
        self._db_target_text: str | None = None
        self._current_job_label: str | None = None
        self._voice_paths_by_tree_id: dict[str, Path] = {}

        self._build_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_worker_thread()
        self.root.after(POLL_MS, self._drain_queue)
        self.root.after(2000, self._tick_connection_watchdog)

    # ---- UI construction -------------------------------------------------

    def _build_widgets(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        progress_row = ttk.Frame(outer)
        progress_row.pack(fill=tk.X)
        self.progress = ttk.Progressbar(progress_row, mode="determinate", maximum=100)
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.progress_label = ttk.Label(progress_row, text="", width=6, anchor=tk.E)
        self.progress_label.pack(side=tk.RIGHT, padx=(8, 0))

        self.status_var = tk.StringVar(value="กำลังเริ่มโปรแกรม…")
        ttk.Label(outer, textvariable=self.status_var).pack(fill=tk.X, pady=(6, 0))

        connection_row = ttk.Frame(outer)
        connection_row.pack(fill=tk.X, pady=(6, 8))
        self.connection_dot = tk.Canvas(connection_row, width=10, height=10, highlightthickness=0)
        self.connection_dot.pack(side=tk.LEFT)
        self._dot_item = self.connection_dot.create_oval(1, 1, 9, 9, fill="#9e9e9e", outline="")
        self.connection_var = tk.StringVar(value="Connecting…")
        ttk.Label(connection_row, textvariable=self.connection_var).pack(side=tk.LEFT, padx=(6, 0))

        log_frame = ttk.LabelFrame(outer, text="Log")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        self.log_text = ScrolledText(log_frame, height=12, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.tag_config("ERROR", foreground="#c0392b")
        self.log_text.tag_config("WARNING", foreground="#b8860b")

        button_row = ttk.Frame(outer)
        button_row.pack(fill=tk.X)
        ttk.Button(button_row, text="ตั้งค่า", command=self._on_settings).pack(side=tk.LEFT)

    # ---- Background worker thread -----------------------------------------

    def _start_worker_thread(self) -> None:
        # Daemon: if construction hangs on a slow/unreachable DB before a
        # Worker even exists, there is no clean way to interrupt a blocking
        # connect() call. A daemon thread guarantees the process can still
        # exit instead of hanging forever; the graceful-shutdown path below
        # is still what handles the far more common idle/rendering cases.
        self.worker_thread = threading.Thread(target=self._worker_thread_main, daemon=True)
        self.worker_thread.start()

    def _worker_thread_main(self) -> None:
        try:
            settings = load_settings()
        except Exception as error:
            self.event_queue.put(("startup_failed", str(error)))
            return
        self.event_queue.put(("settings_loaded", settings))

        configure_logging(settings.work_dir / "worker.log")
        WORKER_LOGGER.addHandler(QueueLogHandler(self.event_queue))

        events = WorkerEvents(
            on_activity=lambda: self.event_queue.put(("activity", time.time())),
            on_progress=lambda job_id, total, completed, current: self.event_queue.put(
                ("progress", job_id, total, completed, current)
            ),
            on_job_started=lambda job: self.event_queue.put(("job_started", job)),
            on_job_completed=lambda job_id, duration: self.event_queue.put(("job_completed", job_id, duration)),
            on_job_failed=lambda job_id, message, code: self.event_queue.put(("job_failed", job_id, message, code)),
        )

        try:
            worker = Worker(settings, events=events)
        except AlreadyRunningError as error:
            self.event_queue.put(("already_running", str(error)))
            return
        except Exception as error:
            self.event_queue.put(("startup_failed", str(error)))
            return

        self.worker = worker
        self.event_queue.put(("worker_ready", describe_database_target(settings.database_url)))
        try:
            worker.run()
        finally:
            worker.close()
            self.event_queue.put(("worker_stopped",))

    # ---- Queue drain / dispatch --------------------------------------------

    def _drain_queue(self) -> None:
        while True:
            try:
                item = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self._dispatch(item)
        if not self._closing:
            self.root.after(POLL_MS, self._drain_queue)

    def _dispatch(self, item: tuple) -> None:
        kind = item[0]
        if kind == "log":
            self._append_log(item[1], item[2])
        elif kind == "activity":
            self._last_activity_at = item[1]
            if self._connection_state != "error":
                self._set_connection_state("connected")
        elif kind == "progress":
            self._update_progress(item[2], item[3], item[4])
        elif kind == "job_started":
            self._on_job_started(item[1])
        elif kind == "job_completed":
            self._current_job_label = None
            self.status_var.set("ว่าง — รอรายการถัดไป…")
            self.progress.configure(value=0)
            self.progress_label.configure(text="")
        elif kind == "job_failed":
            self._current_job_label = None
            _, _job_id, _message, code = item
            self.status_var.set(f"งานล่าสุดล้มเหลว ({code}) — รอรายการถัดไป…")
            self.progress.configure(value=0)
            self.progress_label.configure(text="")
        elif kind == "worker_ready":
            self._db_target_text = item[1]
            self.status_var.set("ว่าง — รอรายการถัดไป…")
            self._set_connection_state("connected")
        elif kind == "settings_loaded":
            self.settings = item[1]
            self.voice_render_settings = VoiceRenderSettingsStore(self.settings.voice_variants_path)
        elif kind == "worker_stopped":
            pass
        elif kind == "already_running":
            # The desktop window remains useful as a local voice-settings
            # editor while the scheduled headless worker owns the GPU/lock.
            # Timing changes are picked up by that worker on its next job.
            self.status_var.set("โหมดตั้งค่า — worker หลักกำลังทำงานอยู่")
            self._set_connection_state("stale")
            messagebox.showinfo("โหมดตั้งค่า", "worker กำลังทำงานอยู่ จึงเปิดหน้าตั้งค่าโดยไม่สร้าง worker ซ้ำ")
        elif kind == "startup_failed":
            self._set_connection_state("error")
            self.status_var.set("เริ่มโปรแกรมไม่สำเร็จ — ดูรายละเอียดในกล่องข้อความ")
            messagebox.showerror("เริ่มโปรแกรมไม่สำเร็จ", item[1])

    def _append_log(self, levelno: int, message: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        tag = logging.getLevelName(levelno) if levelno >= logging.WARNING else None
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"{timestamp}  {message}\n"
        self.log_text.insert(tk.END, line, tag or ())
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > LOG_PANEL_MAX_LINES:
            self.log_text.delete("1.0", f"{line_count - LOG_PANEL_MAX_LINES}.0")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)
        if levelno >= logging.ERROR:
            self._set_connection_state("error")

    def _update_progress(self, total: int, completed: int, current: int | None) -> None:
        percent = int(completed / total * 100) if total > 0 else 0
        self.progress.configure(value=percent)
        self.progress_label.configure(text=f"{percent}%")
        label = self._current_job_label or "กำลังสร้างเสียง"
        if current is not None:
            self.status_var.set(f"{label} (บล็อกที่ {current}/{total})")

    def _on_job_started(self, job: ClaimedJob) -> None:
        self._current_job_label = f"กำลังสร้างเสียง {_job_label(job)}"
        self.status_var.set(self._current_job_label)
        self.progress.configure(value=0)
        self.progress_label.configure(text="0%")

    def _set_connection_state(self, state: str) -> None:
        self._connection_state = state
        colors = {"connecting": "#9e9e9e", "connected": "#2e7d32", "error": "#c0392b", "stale": "#b8860b"}
        labels = {
            "connecting": "Connecting…",
            "connected": "Connected",
            "error": "Connection issue",
            "stale": "No recent activity",
        }
        self.connection_dot.itemconfig(self._dot_item, fill=colors.get(state, "#9e9e9e"))
        suffix = f" — {self._db_target_text}" if self._db_target_text else ""
        if state == "connected" and self._last_activity_at:
            when = datetime.fromtimestamp(self._last_activity_at).strftime("%H:%M:%S")
            suffix += f" (last activity {when})"
        self.connection_var.set(labels.get(state, state) + suffix)

    def _tick_connection_watchdog(self) -> None:
        if self._closing:
            return
        if (
            self._connection_state == "connected"
            and self._last_activity_at is not None
            and time.time() - self._last_activity_at > STALE_ACTIVITY_SECONDS
        ):
            self._set_connection_state("stale")
        self.root.after(2000, self._tick_connection_watchdog)

    # ---- Buttons ------------------------------------------------------------

    def _on_settings(self) -> None:
        if self._current_settings() is None:
            messagebox.showinfo("ตั้งค่า", "โปรแกรมยังไม่พร้อม กรุณารอสักครู่แล้วลองใหม่")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("ตั้งค่า")
        dialog.geometry("700x760")
        dialog.minsize(620, 560)
        dialog.transient(self.root)

        outer = ttk.Frame(dialog, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        # รายละเอียด connection/.env มีประโยชน์เวลาตรวจปัญหา แต่ไม่ควรกิน
        # พื้นที่ส่วนตั้งจังหวะเสียงทุกครั้งที่เปิดหน้าต่าง จึงเริ่มแบบยุบไว้
        # และให้ผู้ใช้กดเปิดเฉพาะเมื่อต้องการดูจริง ๆ.
        info_frame = ttk.LabelFrame(outer, text="ข้อมูลระบบและ .env")
        info_text = ScrolledText(info_frame, wrap=tk.WORD, height=9)
        info_text.pack(fill=tk.X, padx=6, pady=(6, 4))
        self._populate_settings_info(info_text)

        def open_env_folder() -> None:
            env_file = resolve_env_file()
            folder = env_file.parent if env_file.parent.is_dir() else Path.cwd()
            try:
                os.startfile(str(folder))  # noqa: S606 -- local folder only, Windows-only tool
            except Exception as error:
                messagebox.showerror("เปิดโฟลเดอร์ไม่สำเร็จ", str(error))

        ttk.Button(info_frame, text="เปิดโฟลเดอร์ .env", command=open_env_folder).pack(anchor=tk.W, padx=6, pady=(0, 6))
        info_visible = False

        def toggle_info() -> None:
            nonlocal info_visible
            info_visible = not info_visible
            if info_visible:
                info_frame.pack(fill=tk.X, pady=(0, 8), before=toggle_info_button)
                toggle_info_button.configure(text="ซ่อนข้อมูลระบบและ .env")
            else:
                info_frame.pack_forget()
                toggle_info_button.configure(text="แสดงข้อมูลระบบและ .env")

        toggle_info_button = ttk.Button(outer, text="แสดงข้อมูลระบบและ .env", command=toggle_info)
        toggle_info_button.pack(fill=tk.X, pady=(0, 8))

        # สแกน WAV จากทุกโฟลเดอร์จริง ไม่ผูกกับจำนวน Basic slot หรือ naming
        # convention ของ Pro จึงเห็นไฟล์ที่เพิ่ง copy เข้ามาทันทีหลังรีเฟรช.
        tray_frame = ttk.LabelFrame(outer, text="Voice render settings")
        tree = self._build_variant_tree(tray_frame)

        # ส่วนที่ 2: ปุ่มรีเฟรช (ย้ายมาจากหน้าต่างหลัก) + ไปที่โฟลเดอร์ TTSCore
        actions_row = ttk.Frame(outer)
        actions_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(
            actions_row, text="รีเฟรช", command=lambda: self._on_dialog_refresh(info_text, tree)
        ).pack(side=tk.LEFT)
        ttk.Button(
            actions_row, text="ไปที่โฟลเดอร์ TTSCore", command=self._open_ttscore_root
        ).pack(side=tk.LEFT, padx=(8, 0))

        # ส่วนที่ 3: toggle เปิด/ปิด auto-start ตอน boot
        boot_row = ttk.Frame(outer)
        boot_row.pack(fill=tk.X, pady=(0, 8))
        self._build_boot_toggle_row(boot_row)

        self._build_voice_timing_editor(outer, tree)

        # ส่วนที่ 4 (ต่อ): pack Variant Tray ท้ายสุด ให้มันยืดรับพื้นที่ที่เหลือ
        tray_frame.pack(fill=tk.BOTH, expand=True)
        self._populate_variant_tree(tree)

    def _populate_settings_info(self, text: ScrolledText) -> None:
        settings = self._current_settings()
        if settings is None:
            return
        rows = [
            ("Database", describe_database_target(settings.database_url)),
            ("R2 endpoint", settings.r2_endpoint_url),
            ("R2 bucket", settings.r2_bucket_name),
            ("R2 public URL", settings.r2_public_url),
            ("R2 access key ID", _redact_last4(settings.r2_access_key_id)),
            ("R2 secret access key", "(hidden)"),
            ("Model ID", settings.model_id),
            ("Device", settings.device),
            ("Inference timesteps", str(settings.inference_timesteps)),
            ("CFG value", str(settings.cfg_value)),
            ("Max chunk chars", str(settings.max_chunk_chars)),
            ("Max output blocks", str(settings.max_output_blocks)),
            ("Poll seconds", str(settings.poll_seconds)),
            ("Lease seconds", str(settings.lease_seconds)),
            ("Worker ID", settings.worker_id),
            ("Work dir", str(settings.work_dir)),
            ("ffmpeg path", settings.ffmpeg_path),
            ("Published audio", f"MP3 mono · {settings.output_sample_rate / 1_000:g} kHz · {settings.output_mp3_bitrate_kbps} kbps"),
            ("Basic voice folder", str(settings.voice_basic_path)),
            ("Voice variants folder", str(settings.voice_variants_path)),
            (".env in use", str(resolve_env_file())),
        ]
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        for label, value in rows:
            text.insert(tk.END, f"{label}:\n    {value}\n\n")
        text.configure(state=tk.DISABLED)

    def _current_settings(self) -> Settings | None:
        return self.worker.settings if self.worker is not None else self.settings

    def _open_ttscore_root(self) -> None:
        # src/readji_tts/gui.py -> parents[0]=src/readji_tts, [1]=src, [2]=repo root
        root = Path(__file__).resolve().parents[2]
        try:
            os.startfile(str(root))  # noqa: S606 -- local folder only, Windows-only tool
        except Exception as error:
            messagebox.showerror("เปิดโฟลเดอร์ไม่สำเร็จ", str(error))

    def _build_boot_toggle_row(self, parent: ttk.Frame) -> None:
        boot_var = tk.BooleanVar()
        status_var = tk.StringVar()
        checkbox = ttk.Checkbutton(
            parent, text="เปิดใช้งานอัตโนมัติเมื่อเข้าสู่ระบบ (ReadjiTtsWorker)", variable=boot_var
        )

        def refresh_state() -> None:
            try:
                state = get_boot_task_state()
            except Exception as error:
                status_var.set(f"ตรวจสอบสถานะไม่สำเร็จ: {error}")
                checkbox.configure(state=tk.DISABLED)
                return
            if state == "not_found":
                boot_var.set(False)
                status_var.set("ไม่พบ Scheduled Task (ยังไม่ได้ตั้งค่า)")
                checkbox.configure(state=tk.DISABLED)
            else:
                boot_var.set(state == "enabled")
                status_var.set("เปิดอยู่" if state == "enabled" else "ปิดอยู่")
                checkbox.configure(state=tk.NORMAL)

        def on_toggle() -> None:
            try:
                set_boot_task_state(boot_var.get())
            except Exception as error:
                messagebox.showerror("เปลี่ยนสถานะไม่สำเร็จ", str(error))
            # เช็คสถานะจริงซ้ำเสมอไม่ว่าจะสำเร็จหรือพลาด -- checkbox ต้องไม่
            # แสดงผลเพี้ยนไปจากสถานะจริงของระบบหลัง toggle
            refresh_state()

        checkbox.configure(command=on_toggle)
        checkbox.pack(side=tk.LEFT)
        ttk.Label(parent, textvariable=status_var).pack(side=tk.LEFT, padx=(8, 0))
        refresh_state()

    def _build_variant_tree(self, parent: ttk.LabelFrame) -> ttk.Treeview:
        # Treeview row height เป็น style option แยกต่างหากจาก named font ที่
        # _configure_thai_font() ตั้งไว้ -- ต้องตั้งเองไม่งั้นตัวอักษรไทย 13pt
        # จะถูกตัด/ซ้อนทับในแถว
        ttk.Style().configure("Treeview", rowheight=30)
        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        tree = ttk.Treeview(tree_frame, columns=("lead_in", "block_gap", "detail"), show="tree headings")
        tree.heading("#0", text="หมวดหมู่ / ไฟล์")
        tree.heading("lead_in", text="ก่อนเริ่ม")
        tree.heading("block_gap", text="ระหว่างบล็อก")
        tree.heading("detail", text="รายละเอียด")
        tree.column("#0", width=220)
        tree.column("lead_in", width=90, anchor=tk.CENTER)
        tree.column("block_gap", width=100, anchor=tk.CENTER)
        tree.column("detail", width=180)
        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        return tree

    def _populate_variant_tree(self, tree: ttk.Treeview) -> None:
        tree.delete(*tree.get_children())
        settings = self._current_settings()
        if settings is None:
            return
        store = self.voice_render_settings or VoiceRenderSettingsStore(settings.voice_variants_path)
        self.voice_render_settings = store
        try:
            timings = store.load()
            files = scan_voice_reference_files(settings.voice_variants_path)
        except Exception as error:
            WORKER_LOGGER.warning("voice_render_settings_scan_failed error=%s", error)
            messagebox.showerror("สแกนโฟลเดอร์เสียงไม่สำเร็จ", str(error))
            return

        self._voice_paths_by_tree_id = {}
        category_ids: dict[str, str] = {}
        for voice_path in files:
            key = store.key_for(voice_path)
            category = voice_path.parent.relative_to(settings.voice_variants_path.resolve()).as_posix() or "(root)"
            category_id = category_ids.get(category)
            if category_id is None:
                category_id = f"category:{len(category_ids)}"
                category_ids[category] = category_id
                tree.insert("", tk.END, iid=category_id, text=category, open=category.casefold() == "basic", values=("", "", ""))
            timing = timings.get(key, DEFAULT_TIMING)
            voice_id = f"voice:{len(self._voice_paths_by_tree_id)}"
            self._voice_paths_by_tree_id[voice_id] = voice_path
            detail = "ค่าเริ่มต้น" if timing == DEFAULT_TIMING else key
            tree.insert(
                category_id,
                tk.END,
                iid=voice_id,
                text=voice_path.name,
                values=(f"{timing.lead_in_seconds:.2f} วินาที", f"{timing.inter_block_silence_seconds:.2f} วินาที", detail),
            )

    def _build_voice_timing_editor(self, parent: ttk.Frame, tree: ttk.Treeview) -> None:
        frame = ttk.LabelFrame(parent, text="ตั้งจังหวะไฟล์เสียงที่เลือก")
        frame.pack(fill=tk.X, pady=(0, 8))
        selected_var = tk.StringVar(value="เลือกไฟล์ WAV จากรายการด้านล่าง")
        lead_in_var = tk.StringVar(value="0")
        block_gap_var = tk.StringVar(value="0")
        ttk.Label(frame, textvariable=selected_var).grid(row=0, column=0, columnspan=6, sticky=tk.W, padx=8, pady=(6, 4))
        ttk.Label(frame, text="เงียบก่อนเริ่ม (วินาที)").grid(row=1, column=0, sticky=tk.W, padx=(8, 2), pady=(0, 8))
        ttk.Spinbox(frame, from_=0, to=3, increment=0.01, width=7, textvariable=lead_in_var).grid(row=1, column=1, padx=(0, 12), pady=(0, 8))
        ttk.Label(frame, text="เงียบระหว่างบล็อก (วินาที)").grid(row=1, column=2, sticky=tk.W, padx=(0, 2), pady=(0, 8))
        ttk.Spinbox(frame, from_=0, to=3, increment=0.01, width=7, textvariable=block_gap_var).grid(row=1, column=3, padx=(0, 12), pady=(0, 8))

        def selected_voice_path() -> Path | None:
            selection = tree.selection()
            return self._voice_paths_by_tree_id.get(selection[0]) if selection else None

        def on_selected(_: object) -> None:
            voice_path = selected_voice_path()
            if voice_path is None or self.voice_render_settings is None:
                return
            try:
                timing = self.voice_render_settings.timing_for(voice_path)
            except Exception as error:
                messagebox.showerror("อ่านค่าเสียงไม่สำเร็จ", str(error))
                return
            selected_var.set(str(voice_path))
            lead_in_var.set(f"{timing.lead_in_seconds:.2f}")
            block_gap_var.set(f"{timing.inter_block_silence_seconds:.2f}")

        def save() -> None:
            voice_path = selected_voice_path()
            if voice_path is None or self.voice_render_settings is None:
                messagebox.showinfo("ตั้งจังหวะ", "เลือกไฟล์ WAV ที่ต้องการตั้งค่าก่อน")
                return
            try:
                timing = VoiceRenderTiming(
                    lead_in_seconds=float(lead_in_var.get()),
                    inter_block_silence_seconds=float(block_gap_var.get()),
                )
                self.voice_render_settings.save_timing(voice_path, timing)
            except (ValueError, RuntimeError) as error:
                messagebox.showerror("บันทึกไม่สำเร็จ", str(error))
                return
            self._populate_variant_tree(tree)
            WORKER_LOGGER.info("voice_render_timing_saved path=%s lead_in=%.3f inter_block=%.3f", voice_path, timing.lead_in_seconds, timing.inter_block_silence_seconds)
            messagebox.showinfo("ตั้งจังหวะ", "บันทึกแล้ว มีผลกับงานใหม่ที่ worker รับหลังจากนี้")

        ttk.Button(frame, text="คืนค่า 0", command=lambda: (lead_in_var.set("0"), block_gap_var.set("0"))).grid(row=1, column=4, padx=(0, 6), pady=(0, 8))
        ttk.Button(frame, text="บันทึก", command=save).grid(row=1, column=5, padx=(0, 8), pady=(0, 8))
        tree.bind("<<TreeviewSelect>>", on_selected)

    def _on_dialog_refresh(self, info_text: ScrolledText, tree: ttk.Treeview) -> None:
        settings = self._current_settings()
        if settings is None:
            messagebox.showinfo("รีเฟรช", "โปรแกรมยังไม่พร้อม กรุณารอสักครู่แล้วลองใหม่")
            return
        try:
            profiles = load_basic_voice_profiles(settings.voice_basic_path)
        except Exception as error:
            WORKER_LOGGER.warning("voice_profile_refresh_failed error=%s", error)
            messagebox.showerror("รีเฟรชไม่สำเร็จ", str(error))
            return
        # Whole-dict reassignment, never in-place mutation: a concurrent read
        # on the worker thread (self.voice_profiles.get(slot)) always sees a
        # complete old or complete new dict under the GIL, never a torn one.
        # A job already past get_voice_profile() captured its own profile in
        # a local variable, so this never disturbs a render in progress.
        if self.worker is not None:
            self.worker.voice_profiles = profiles
        WORKER_LOGGER.info("voice_profile_refreshed slots=%s", ", ".join(sorted(profiles)))
        self._populate_settings_info(info_text)
        self._populate_variant_tree(tree)
        messagebox.showinfo("รีเฟรช", "โหลดข้อมูลใหม่เรียบร้อยแล้ว")

    # ---- Shutdown -------------------------------------------------------------

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self.worker is not None:
            self.status_var.set("กำลังปิดอย่างปลอดภัย… รอจนกว่าขั้นตอนปัจจุบันจะเสร็จ")
            self.worker.stop()
        else:
            self.status_var.set("กำลังปิด…")
        self._shutdown_deadline = time.time() + SHUTDOWN_TIMEOUT_SECONDS
        self._wait_for_shutdown()

    def _wait_for_shutdown(self) -> None:
        thread_alive = self.worker_thread is not None and self.worker_thread.is_alive()
        if thread_alive and time.time() < self._shutdown_deadline:
            self.root.after(200, self._wait_for_shutdown)
            return
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    _configure_thai_font(root)
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    ReadjiTtsGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
