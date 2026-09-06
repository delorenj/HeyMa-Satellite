import pytest
from pydantic import SecretStr

from tonny_voice.config import Settings


@pytest.fixture
def settings():
    return Settings(
        deepgram_api_key=SecretStr("test-only"),
        cartesia_api_key=SecretStr("test-only"),
        llm_api_key=SecretStr("test-only"),
    )
