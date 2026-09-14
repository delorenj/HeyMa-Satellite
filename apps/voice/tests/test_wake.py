"""Deterministic checks for the exact upstream Hey Tonny classifier."""

import hashlib
import math
import wave
from pathlib import Path

import pytest

from tonny_voice.config import Settings
from tonny_voice.wake import WakeDetector, WakeDetectorError

FIXTURE = Path(__file__).parent / "fixtures" / "hey-tonny-say.wav"
FIXTURE_SHA256 = "5b187dfb003e18aa32e425ba4baf46c9b5537fced348251db752b40175172d90"
MODEL_SHA256 = "558bd199797084e41f6e1e9fd3cd330fb9920af5d55e06d2c647659bab33a5a0"
FRAME_BYTES = 2_560


def fixture_pcm() -> bytes:
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256
    with wave.open(str(FIXTURE), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (
            16_000,
            1,
            2,
        )
        return audio.readframes(audio.getnframes())


def test_exact_custom_model_detects_synthetic_hey_tonny() -> None:
    """Fixture provenance: macOS `say`, converted to PCM16 mono 16 kHz on 2026-09-08."""
    detector = WakeDetector(Settings())
    assert (
        hashlib.sha256(detector.settings.wake_model_path.read_bytes()).hexdigest() == MODEL_SHA256
    )
    detector.reset()
    pcm = fixture_pcm()
    scores = [
        detector.score(pcm[offset : offset + FRAME_BYTES])
        for offset in range(0, len(pcm), FRAME_BYTES)
    ]
    consecutive = 0
    longest_run = 0
    for score in scores:
        consecutive = consecutive + 1 if score >= detector.settings.wake_threshold else 0
        longest_run = max(longest_run, consecutive)
    assert longest_run >= detector.settings.wake_trigger_frames


def test_thirty_seconds_of_digital_silence_never_crosses_threshold() -> None:
    detector = WakeDetector(Settings())
    detector.reset()
    frame = b"\0" * FRAME_BYTES
    scores = [detector.score(frame) for _ in range(30 * 1_000 // 80)]
    assert all(
        math.isfinite(score) and 0 <= score < detector.settings.wake_threshold for score in scores
    )


def test_invalid_or_missing_custom_model_fails_gateway_startup(tmp_path: Path) -> None:
    for field in (
        "wake_model_sha256",
        "wake_melspec_model_sha256",
        "wake_embedding_model_sha256",
    ):
        with pytest.raises(WakeDetectorError, match="wake_model_checksum_mismatch"):
            WakeDetector(Settings(**{field: "0" * 64}))
    with pytest.raises(WakeDetectorError, match="wake_model_missing"):
        WakeDetector(Settings(wake_model_path=tmp_path / "missing.onnx"))


class FakeModel:
    def __init__(self, **kwargs):
        class Port:
            def __init__(self, name, shape):
                self.name = name
                self.type = "tensor(float)"
                self.shape = shape

        class Session:
            def get_inputs(self):
                return [Port("embeddings", [1, 16, 96])]

            def get_outputs(self):
                return [Port("score", [1, 1])]

        self.models = {Path(kwargs["wakeword_models"][0]).stem: Session()}
        self.value = 0.25
        self.reset_calls = 0

    def predict(self, _samples):
        return {"hey_tonny": self.value}

    def reset(self):
        self.reset_calls += 1


def test_adapter_rejects_invalid_pcm_and_scores() -> None:
    detector = WakeDetector(Settings(), model_factory=FakeModel)
    assert detector.score(b"\0\0" * 1_280) == 0.25
    for pcm in (b"", b"\0"):
        with pytest.raises(WakeDetectorError, match="wake_audio_invalid"):
            detector.score(pcm)
    for value in (float("nan"), -0.1, 1.1):
        detector._model.value = value
        with pytest.raises(WakeDetectorError, match="wake_score_invalid"):
            detector.score(b"\0\0")


def test_loadable_model_with_wrong_classifier_signature_fails_startup() -> None:
    settings = Settings()
    wrong_model = settings.wake_embedding_model_path
    with pytest.raises(WakeDetectorError, match="wake_model_invalid"):
        WakeDetector(
            settings.model_copy(
                update={
                    "wake_model_path": wrong_model,
                    "wake_model_sha256": hashlib.sha256(wrong_model.read_bytes()).hexdigest(),
                }
            )
        )
