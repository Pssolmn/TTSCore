from __future__ import annotations

import csv
import io
import subprocess
from typing import Literal

TASK_NAME = "ReadjiTtsWorker"
BootTaskState = Literal["enabled", "disabled", "not_found"]

# Defensive only -- a local Task Scheduler query/change is near-instant. This
# just bounds the worst case so a hung Task Scheduler service can't freeze
# the whole Tk mainloop (which also drives the log panel and worker events)
# indefinitely over a settings checkbox.
_SUBPROCESS_TIMEOUT_SECONDS = 10


def _run_schtasks(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        text=True,
        # schtasks.exe is a console tool and writes output in the console OEM
        # code page, not UTF-8.
        encoding="oem",
        errors="replace",
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        # The GUI runs under pythonw.exe (no console). Without this flag,
        # spawning a console-subsystem exe like schtasks briefly flashes a
        # new console window.
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def get_boot_task_state(task_name: str = TASK_NAME) -> BootTaskState:
    result = _run_schtasks(["/Query", "/TN", task_name, "/FO", "CSV", "/NH"])
    if result.returncode != 0:
        # schtasks gives no structured "not found" exit code via CSV, only
        # this human-readable stderr line. Accepted as-is: this project is
        # already Windows-only, single-machine, with no localization need.
        if "cannot find the file specified" in (result.stderr or "").casefold():
            return "not_found"
        raise RuntimeError(f"schtasks query failed: {result.stderr or result.stdout}")
    rows = list(csv.reader(io.StringIO(result.stdout)))
    if not rows or not rows[0]:
        raise RuntimeError(f"schtasks query returned no data for task {task_name!r}")
    status = rows[0][-1].strip()
    return "disabled" if status.casefold() == "disabled" else "enabled"


def set_boot_task_state(enabled: bool, task_name: str = TASK_NAME) -> None:
    current = get_boot_task_state(task_name)
    if current == "not_found":
        raise RuntimeError(f"Scheduled task {task_name!r} does not exist; nothing to enable/disable")
    flag = "/ENABLE" if enabled else "/DISABLE"
    result = _run_schtasks(["/Change", "/TN", task_name, flag])
    if result.returncode != 0:
        raise RuntimeError(f"schtasks change failed: {result.stderr or result.stdout}")
