from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import numpy as np



class VoiceSynthesisSettings(Protocol):
    """The small configuration surface VoxCPM needs for local synthesis."""

    model_id: str
    device: str
    load_denoiser: bool
    optimize: bool
    cfg_value: float
    inference_timesteps: int
    badcase_max_attempts: int


class VoxCpmNarrator:
    def __init__(self, settings: VoiceSynthesisSettings) -> None:
        try:
            import torch
            from voxcpm import VoxCPM
        except ImportError as error:
            raise RuntimeError("VoxCPM2 dependencies are not installed. Follow TTSCore/README.md.") from error
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available. The Readji worker intentionally refuses CPU synthesis.")
        self.settings = settings
        self.model = VoxCPM.from_pretrained(
            settings.model_id,
            device=settings.device,
            load_denoiser=settings.load_denoiser,
            optimize=settings.optimize,
        )
        self.sample_rate = int(self.model.tts_model.sample_rate)

    def synthesize(self, text: str, reference_wav_path: Path) -> np.ndarray:
        audio = self.model.generate(
            text=text,
            reference_wav_path=str(reference_wav_path),
            cfg_value=self.settings.cfg_value,
            inference_timesteps=self.settings.inference_timesteps,
            # VoxCPM2 otherwise retries a malformed result up to three full
            # renders. Two attempts preserve a quality retry while bounding
            # the worst-case GPU time for one paragraph.
            retry_badcase=True,
            retry_badcase_max_times=self.settings.badcase_max_attempts,
        )
        return np.asarray(audio)

    def design_voice(self, text: str, description: str) -> np.ndarray:
        audio = self.model.generate(
            text=f"({description}) {text}",
            cfg_value=self.settings.cfg_value,
            inference_timesteps=self.settings.inference_timesteps,
        )
        return np.asarray(audio)

    def release_cuda_cache(self) -> None:
        """Best-effort memory recovery after an out-of-memory render failure."""
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            # A cleanup failure must never hide the original synthesis error.
            return
