# Tonny voice gateway

The Pi captures PCM and plays a WAV. This Python service runs Pipecat speech
services and a standalone Cartesia Line `LlmAgent` on the server:

```text
Pi microphone -> /v1/voice -> Pipecat Deepgram STT
                            -> Line LlmAgent / OpenRouter
                            -> Pipecat Cartesia TTS -> WAV -> Pi speaker
```

This is a buffered, one-utterance-at-a-time walking skeleton. The satellite
supplies the turn boundary; no server wake word or VAD model is loaded. There
are no CRM, telephony, customer records, device-control tools, or client-project
imports. The Line SDK is used directly; Cartesia's managed phone service and
LiveKit are not part of this path.

## Run

Python 3.12 and uv are required. Resolve secrets into the process environment;
do not write resolved values into files.

```sh
uv sync --project apps/voice --frozen
op run --env-file deploy/tonny/voice.env.op -- \
  uv run --project apps/voice tonny-voice --host 0.0.0.0 --port 18778
```

| Environment variable | Purpose / default |
|---|---|
| `TONNY_DEEPGRAM_API_KEY` | Required STT credential |
| `TONNY_CARTESIA_API_KEY` | Required TTS credential |
| `TONNY_LLM_API_KEY` | Required OpenRouter credential |
| `TONNY_LLM_MODEL` | `openrouter/openai/gpt-4.1-mini`; an `openai/...` ID is also accepted |
| `TONNY_DEEPGRAM_MODEL` | `nova-3-general` |
| `TONNY_CARTESIA_MODEL` | `sonic-3` |
| `TONNY_CARTESIA_VOICE_ID` | Skylar: `db6b0ed5-d5d3-463d-ae85-518a07d3c2b4` |
| `TONNY_REVISION` | Immutable deployed source revision, reported by `/healthz` |
| `TONNY_MAX_INPUT_SECONDS` | 20 seconds, PCM 16 kHz mono |
| `TONNY_RESPONSE_TIMEOUT_SECONDS` | 30 seconds after `end_of_input` |
| `TONNY_HISTORY_TURNS` | 6 completed exchanges held in memory |
| `TONNY_HISTORY_TTL_SECONDS` | 900 seconds of inactivity clears conversation |

## Protocol and evidence

The websocket matches `docs/wire-contract.md`: UUID4 `hello`, `ready`, binary
PCM S16_LE at 16 kHz mono, `end_of_input`, `response_start` with `format: wav`,
WAV bytes, then `response_end`. The server closes after one response. Both
`client: heyma` and `client: tonny` are accepted. At most one session is active;
a second receives `busy`. Faster-than-real-time uploads are supported within
the duration and byte limits.

Pipecat input audio normally uses a priority system queue. Buffered PCM travels
in ordinary FIFO frames here, and becomes a Pipecat audio frame inside the STT
processor. Deepgram receives `CloseStream` only after all input was sent. The
gateway waits for terminal `Metadata` and verifies its duration before using
the finalized transcript. A first `from_finalize` result is insufficient: a
live test returned only the first 0.4 seconds of a 2.6-second upload that way.

`GET /healthz` separates provider configuration from counters for actual STT,
LLM, TTS and websocket response completion. A configured key does not prove
provider capability. The protocol does not acknowledge physical playback, so
the gateway cannot claim the user heard a response. Conversation is committed
only after `response_end` is sent and disappears on restart. `POST /v1/reset`
clears it while idle.

The public websocket is intended for the owner's LAN. The 16 kHz mono protocol
does not carry client authentication or support concurrent speakers or barge-in.

## Verify

```sh
uv run --project apps/voice pytest apps/voice/tests -q
uv run --project apps/voice ruff check apps/voice
```

Tests run real Pipecat workers and the real Line SDK around controlled provider
fakes. They cover complete WAV output, finalization ordering, two-turn model
history, silence, provider failure, disconnect cancellation, timeouts and wire
limits. Provider, network, microphone and speaker acceptance require live runs.
