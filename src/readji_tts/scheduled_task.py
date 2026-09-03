from __future__ import annotations

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


def _run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _powershell_literal(value: str) -> str:
    """Return a non-interpolating PowerShell string literal."""
    return "'" + value.replace("'", "''") + "'"


def get_boot_task_state(task_name: str = TASK_NAME) -> BootTaskState:
    # schtasks.exe reports both "not found" and Disabled using localized text,
    # which made this checkbox fail on a Thai customer installation. Emit one
    # of our own stable tokens from the Windows ScheduledTasks API instead.
    name = _powershell_literal(task_name)
    result = _run_powershell(
        f"$task = Get-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue; "
        "if ($null -eq $task) { 'not_found' } "
        "elseif ($task.State -eq 'Disabled') { 'disabled' } "
        "else { 'enabled' }"
    )
    if result.returncode != 0:
        raise RuntimeError(f"Scheduled Task query failed: {result.stderr or result.stdout}")
    state = (result.stdout or "").strip()
    if state not in {"enabled", "disabled", "not_found"}:
        raise RuntimeError(f"Scheduled Task query returned invalid state for {task_name!r}: {state!r}")
    return state  # type: ignore[return-value]


def set_boot_task_state(enabled: bool, task_name: str = TASK_NAME) -> None:
    current = get_boot_task_state(task_name)
    if current == "not_found":
        raise RuntimeError(f"Scheduled task {task_name!r} does not exist; nothing to enable/disable")
    flag = "/ENABLE" if enabled else "/DISABLE"
    result = _run_schtasks(["/Change", "/TN", task_name, flag])
    if result.returncode != 0:
        raise RuntimeError(f"schtasks change failed: {result.stderr or result.stdout}")
