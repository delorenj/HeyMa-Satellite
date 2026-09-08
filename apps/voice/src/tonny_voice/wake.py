"""Server-side openWakeWord adapter. This module is never shipped to Tonny."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from tonny_voice.config import Settings


class WakeDetectorError(RuntimeError):
    """A fixed startup/runtime failure safe to surface without model internals."""


@dataclass(frozen=True)
class WakeDetection:
    model: str
    score: float


class WakeScorer(Protocol):
    model_name: str

    def score(self, pcm: bytes) -> float: ...

    def reset(self) -> None: ...


class WakeDetector:
    """Own one stateful openWakeWord pipeline for the single active session."""

    def __init__(
        self,
        settings: Settings,
        *,
        model_factory: Callable[..., object] | None = None,
    ) -> None:
        self.settings = settings
        self.model_name = settings.wake_model_path.stem
        self._validate_assets()
        if model_factory is None:
            try:
                from openwakeword.model import Model
            except ImportError as exc:
                raise WakeDetectorError("wake_dependency_unavailable") from exc
            model_factory = Model
        try:
            random_state = np.random.get_state()
            np.random.seed(0)
            try:
                self._model = model_factory(
                    wakeword_models=[str(settings.wake_model_path)],
                    inference_framework="onnx",
                    melspec_model_path=str(settings.wake_melspec_model_path),
                    embedding_model_path=str(settings.wake_embedding_model_path),
                    ncpu=1,
                )
            finally:
                np.random.set_state(random_state)
        except Exception as exc:
            raise WakeDetectorError("wake_model_invalid") from exc

        models = getattr(self._model, "models", {})
        if self.model_name not in models:
            raise WakeDetectorError("wake_model_name_mismatch")
        self._validate_runtime_model(models[self.model_name])

    def _validate_runtime_model(self, session: object) -> None:
        try:
            inputs = session.get_inputs()
            outputs = session.get_outputs()
            valid_signature = (
                len(inputs) == 1
                and inputs[0].name == "embeddings"
                and inputs[0].type == "tensor(float)"
                and inputs[0].shape == [1, 16, 96]
                and len(outputs) == 1
                and outputs[0].name == "score"
                and outputs[0].type == "tensor(float)"
                and outputs[0].shape == [1, 1]
            )
            if not valid_signature:
                raise ValueError("unexpected classifier signature")
            score = self.score(b"\0" * 2_560)
            if not math.isfinite(score):
                raise ValueError("non-finite startup score")
            self.reset()
        except Exception as exc:
            raise WakeDetectorError("wake_model_invalid") from exc

    def _validate_assets(self) -> None:
        paths = (
            self.settings.wake_model_path,
            self.settings.wake_melspec_model_path,
            self.settings.wake_embedding_model_path,
        )
        if any(not path.is_file() for path in paths):
            raise WakeDetectorError("wake_model_missing")
        if any(path.stat().st_size <= 0 for path in paths):
            raise WakeDetectorError("wake_model_empty")
        digest = hashlib.sha256(self.settings.wake_model_path.read_bytes()).hexdigest()
        if digest != self.settings.wake_model_sha256:
            raise WakeDetectorError("wake_model_checksum_mismatch")

    def score(self, pcm: bytes) -> float:
        if not pcm or len(pcm) % 2:
            raise WakeDetectorError("wake_audio_invalid")
        try:
            samples = np.frombuffer(pcm, dtype="<i2")
            predictions = self._model.predict(samples)
            score = float(predictions[self.model_name])
        except Exception as exc:
            raise WakeDetectorError("wake_inference_failed") from exc
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise WakeDetectorError("wake_score_invalid")
        return score

    def reset(self) -> None:
        try:
            random_state = np.random.get_state()
            np.random.seed(0)
            try:
                self._model.reset()
            finally:
                np.random.set_state(random_state)
        except Exception as exc:
            raise WakeDetectorError("wake_reset_failed") from exc
