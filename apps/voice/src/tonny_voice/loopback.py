"""Provider-free audio loopback and the reply/error seam shared by both engines."""

import io
import wave
from collections import deque
from dataclasses import dataclass

from tonny_voice.config import Settings


class VoiceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Reply:
    transcript: str | None
    text: str | None
    wav: bytes


class LoopbackEngine:
    """Return exact PCM samples in a WAV; no speech or model processing."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.history: deque[tuple[str, str]] = deque(maxlen=0)
        self.evidence = {"stt_turns": 0, "llm_turns": 0, "tts_turns": 0}

    async def process(self, pcm: bytes) -> Reply:
        if len(pcm) + 44 > self.settings.max_output_bytes:
            raise VoiceError("response_too_large", "The loopback WAV exceeds the output limit.")
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(pcm)
        return Reply(transcript=None, text=None, wav=output.getvalue())

    def remember(self, reply: Reply) -> None:
        # Loopback proves audio transport, not a conversation.
        pass

    def reset(self) -> None:
        self.history.clear()
