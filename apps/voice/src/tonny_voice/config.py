"""Secrets come only from the launch environment (normally supplied by op run)."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TONNY_", extra="ignore")

    deepgram_api_key: SecretStr = SecretStr("")
    cartesia_api_key: SecretStr = SecretStr("")
    llm_api_key: SecretStr = SecretStr("")
    deepgram_model: str = "nova-3-general"
    llm_model: str = "openrouter/openai/gpt-4.1-mini"
    cartesia_model: str = "sonic-3"
    cartesia_voice_id: str = "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"
    commit: str = ""
    revision: str = ""

    max_input_seconds: int = Field(default=20, ge=1, le=60)
    max_frame_bytes: int = Field(default=65536, ge=2560, le=262144)
    max_output_bytes: int = Field(default=4 * 1024 * 1024, ge=4096, le=8 * 1024 * 1024)
    max_text_chars: int = Field(default=1600, ge=80, le=4000)
    history_turns: int = Field(default=6, ge=0, le=20)
    history_ttl_seconds: float = Field(default=900, gt=0, le=86400)
    hello_timeout_seconds: float = Field(default=5, gt=0, le=10)
    input_timeout_seconds: float = Field(default=30, gt=0, le=90)
    response_timeout_seconds: float = Field(default=30, gt=0, le=45)
    stt_timeout_seconds: float = Field(default=10, gt=0, le=20)
    llm_timeout_seconds: float = Field(default=10, gt=0, le=20)
    tts_timeout_seconds: float = Field(default=10, gt=0, le=20)

    @property
    def configured(self) -> dict[str, bool]:
        return {
            "deepgram": bool(self.deepgram_api_key.get_secret_value()),
            "openrouter": bool(self.llm_api_key.get_secret_value()),
            "cartesia": bool(self.cartesia_api_key.get_secret_value() and self.cartesia_voice_id),
        }

    @property
    def max_input_bytes(self) -> int:
        return self.max_input_seconds * 16000 * 2
