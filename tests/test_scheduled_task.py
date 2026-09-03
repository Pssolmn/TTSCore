import subprocess

import pytest

from readji_tts import scheduled_task


@pytest.mark.parametrize("state", ["enabled", "disabled", "not_found"])
def test_boot_task_state_uses_language_independent_tokens(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    monkeypatch.setattr(
        scheduled_task,
        "_run_powershell",
        lambda _script: subprocess.CompletedProcess([], 0, stdout=state + "\n", stderr=""),
    )

    assert scheduled_task.get_boot_task_state("ReadjiTtsWorker") == state


def test_boot_task_state_rejects_unexpected_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scheduled_task,
        "_run_powershell",
        lambda _script: subprocess.CompletedProcess([], 0, stdout="สถานะไม่รู้จัก\n", stderr=""),
    )

    with pytest.raises(RuntimeError, match="invalid state"):
        scheduled_task.get_boot_task_state()
