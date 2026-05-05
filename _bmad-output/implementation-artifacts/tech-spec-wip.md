---
title: 'HeyMa Voice Satellite'
slug: 'heyma-satellite'
created: '2026-03-25'
updated: '2026-05-05'
status: 'in-progress'
stepsCompleted: [1, 2]
tech_stack: ['python', 'uv', 'openwakeword', 'sounddevice', 'websockets', 'pydantic-settings', 'systemd']
files_to_modify:
  - 'heyma-satellite/' (new package)
  - 'satellite/systemd/heyma.service' (new)
  - 'satellite/scripts/deploy.sh' (new)
  - 'docs/wire-contract.md' (new)
  - 'pyproject.toml' (new, uv-managed)
  - 'custom_wakewords/' (existing, populated by training task)
  - 'tonny.local: disable wyoming-satellite + wyoming-openwakeword units' (decommission)
code_patterns:
  - 'asyncio main loop with sounddevice InputStream callback'
  - 'websockets async client with exponential reconnect'
  - 'openwakeword Model.predict on rolling 80ms frame buffer'
  - 'pydantic-settings BaseSettings reading heyma_* env vars'
test_patterns:
  - 'pytest-asyncio with FakeWebSocketServer (websockets.serve fixture)'
  - 'sounddevice mocked via numpy frame fixtures'
  - 'openwakeword stubbed Model that fires on test sentinel'
---

# Tech-Spec: HeyMa Voice Satellite

**Updated:** 2026-05-05 (gutted from prior multi-service design)

## Overview

### Problem Statement

The Pi Zero needs a single, focused job: be a dumb voice satellite. Wake on "hey tonny", stream mic audio to a single upstream endpoint, play whatever WAV comes back. The previous spec accreted Wyoming-protocol plumbing, server-side STT shims, Bloodbank routing, and TTS bridges that do not belong on (or near) this hardware. Every layer of that stack is upstream's concern.

### Solution

A single Python package, `heyma-satellite/`, runs as one systemd unit on `tonny.local`. It uses:

- `openwakeword` to detect "hey tonny" locally on a rolling audio buffer.
- `sounddevice` for mic capture and speaker playback against the ReSpeaker HAT (or USB mic fallback).
- `websockets` to connect to the upstream `heyma-gateway` at `ws://192.168.1.12:<port>/v1/voice`, stream raw PCM during an utterance, and play the binary WAV the gateway sends back.

That replaces `wyoming-satellite`, `wyoming-openwakeword`, and all proposed server-side custom code from this repo. Everything STT/agent/TTS lives behind the `heyma-gateway` boundary in .

### Scope

**In Scope:**

- Pi-side audio capture, wake detection, end-of-utterance detection, transport, and playback.
- WebSocket wire contract documentation (`docs/wire-contract.md`).
- systemd unit, deploy script, and decommission of the existing Wyoming units.
- Unit tests for the four module seams (audio, wake, gateway, config).

**Out of Scope:**

- `heyma-gateway` implementation (separate repo, separate spec).
- STT engine, agent orchestration, TTS engine selection.
- Wake word model training (`hey_tonny_training/` already covers it).
- Audio observability, distributed tracing, structured log shipping (deferred).

## Context for Development

### Assumptions (correct me before I proceed)

1. `heyma-gateway` will be built in parallel and conform to the wire contract below. HeyMa Voice Satellite can be built and unit-tested against a fake gateway before the real one ships.
2. The trained `hey_tonny.tflite` lands at `/home/delorenj/custom_wakewords/hey_tonny.tflite` on the Pi.
3. ReSpeaker capture device matches the pattern in `rec.py` (16 kHz mono, S16_LE, `plughw:wm8960soundcard`). USB-mic fallback is selectable via env var, not auto-detected.
4. End-of-utterance is detected by simple RMS silence threshold + minimum-utterance guard. We can swap in `webrtcvad` later if accuracy is insufficient.

### Codebase Patterns

- Pi resource ceiling: 512 MB RAM, no ML inference beyond openwakeword's small models.
- Network is unreliable. The client must reconnect with exponential backoff and never crash the systemd unit on transient failure.
- Audio device must be released cleanly on every shutdown (`finally:` blocks around streams).
- All config via `heyma_*` env vars, hydrated through pydantic-settings.

### Files to Reference

| File                                                           | Purpose                                                                                        |
| -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `rec.py`                                                       | Working sounddevice ReSpeaker capture pattern (sample rate, device selection, S16_LE encoding) |
| `hey_tonny_training/TRAINING_GUIDE.txt`                        | Wake word model production workflow                                                            |
| `tonny.local:/etc/systemd/system/wyoming-satellite.service`    | Existing unit to be stopped + disabled                                                         |
| `tonny.local:/etc/systemd/system/wyoming-openwakeword.service` | Existing unit to be stopped + disabled                                                         |

### Technical Decisions

- **WebSocket bidirectional over HTTP request/response:** lower latency (gateway can begin synthesis before the user finishes speaking), one persistent connection with clear lifecycle, single endpoint to monitor.
- **No Wyoming on the Pi:** the Wyoming protocol's value is multi-service routing (ASR, intent, TTS). Since the gateway collapses all of that behind one WS endpoint, Wyoming's surface area is pure overhead. Enumerated alternatives (`wyoming-faster-whisper`, `wyoming-whisper-cpp`, `wyoming-vosk`, `wyoming-nemo-asr`) offer no quality advantage over what `heyma-gateway` can run internally.
- **`openwakeword` Python lib direct:** no Wyoming wrapper, no IPC. Single process owns the whole audio pipeline.
- **`heyma-gateway` as named peer:** the wire contract is the only thing HeyMa knows about it. The gateway can be replaced or rearchitected freely without Pi changes.

## Implementation Plan

### Tasks

**Phase 1 - Core package**

- [ ] `pyproject.toml` -- uv-managed, deps: `openwakeword`, `sounddevice`, `websockets`, `pydantic-settings`, `numpy`. Test deps: `pytest`, `pytest-asyncio`.
- [ ] `heyma/config.py` -- `Settings(BaseSettings)`: `gateway_url`, `wake_model_path`, `wake_threshold`, `mic_device`, `speaker_device`, `sample_rate=16000`, `silence_threshold_db`, `min_utterance_ms`, `max_utterance_ms`.
- [ ] `heyma/audio.py` -- async mic stream yielding 80 ms PCM frames, async speaker player accepting WAV bytes.
- [ ] `heyma/wakeword.py` -- `WakeDetector` wrapping `openwakeword.Model`, exposes `async detect(frame_iter)` that yields when threshold is crossed.
- [ ] `heyma/utterance.py` -- RMS-based end-of-utterance detector with min/max guards, returns when silence sustained.
- [ ] `heyma/gateway.py` -- `GatewayClient`: connect, send hello, stream PCM frames, send `end_of_input`, await binary WAV, reconnect with exponential backoff on transport failures.
- [ ] `heyma/__main__.py` -- main coroutine wiring the four pieces. Loop: capture frames → wake detect → stream to gateway → play WAV → repeat.

**Phase 2 - Pi deployment**

- [ ] `satellite/systemd/heyma.service` -- `Type=simple`, `Restart=always`, `RestartSec=5`, `ExecStart=/usr/bin/uv run python -m heyma`, `EnvironmentFile=/etc/heyma.env`.
- [ ] `satellite/scripts/deploy.sh` -- idempotent: rsync repo to Pi, `uv sync`, install unit, daemon-reload, restart, tail journalctl for smoke.
- [ ] `satellite/scripts/decommission-wyoming.sh` -- stops and disables `wyoming-satellite.service` + `wyoming-openwakeword.service` on the Pi. Reversible: keeps the unit files in place.

**Phase 3 - Wire contract + tests**

- [ ] `docs/wire-contract.md` -- complete WS protocol doc with message-trace Mermaid diagram (see Wire Contract section below).
- [ ] `tests/test_config.py` -- env var → Settings round-trip, validation errors.
- [ ] `tests/test_wakeword.py` -- stubbed openwakeword Model fires on a sentinel frame.
- [ ] `tests/test_utterance.py` -- silence detection on synthetic sine + silence buffer.
- [ ] `tests/test_gateway.py` -- pytest-asyncio fake `websockets.serve` fixture: assert hello, PCM streamed, end_of_input sent, WAV consumed.
- [ ] `tests/test_main_smoke.py` -- end-to-end with fake gateway and fake mic, asserts one full wake → stream → playback cycle.

### Wire Contract (load-bearing)

```
ws://192.168.1.12:<port>/v1/voice
```

**Open:** client connects, sends JSON:

```json
{
  "type": "hello",
  "session_id": "<uuid>",
  "sample_rate": 16000,
  "encoding": "pcm_s16le",
  "channels": 1,
  "client": "heyma",
  "version": "0.1.0"
}
```

Server replies:

```json
{ "type": "ready", "session_id": "<uuid>" }
```

**Per utterance:**

1. Client sends N binary frames (raw S16_LE PCM, 80 ms each = 2560 bytes at 16 kHz mono).
2. Client sends `{"type":"end_of_input"}` when EOU detected.
3. Server eventually sends `{"type":"response_start","format":"wav"}`.
4. Server sends one or more binary frames forming a complete WAV (header + data).
5. Server sends `{"type":"response_end"}`.

**Errors:** either side may send `{"type":"error","code":"<code>","message":"<msg>"}` then close.
**Close:** `{"type":"close"}` then TCP close.

### Acceptance Criteria

- Given the unit is running and `hey_tonny.tflite` is loaded, when "hey tonny" is spoken, then `journalctl -u heyma` logs a wake event within 500 ms of utterance end.
- Given a wake event, when the user keeps speaking and then stops, then the client opens a WS to the configured gateway, streams PCM frames, and sends `end_of_input` within 250 ms of detected silence.
- Given the gateway returns a `response_start` + WAV + `response_end`, when the WAV is received, then it plays through the configured speaker device with no truncation.
- Given the gateway is unreachable when wake fires, when the client retries, then it backs off (1s, 2s, 4s, 8s, capped at 30s) and never crashes the systemd unit. Logs structured `gateway_unreachable` events.
- Given the gateway disconnects mid-utterance, when the client detects the closed socket, then it aborts the current utterance, flushes audio buffers, returns to wake-listening, and reconnects on next wake.
- Given `decommission-wyoming.sh` has run, when `systemctl status wyoming-satellite` is queried on the Pi, then the unit is `inactive (dead)` and `disabled`. Reversal documented in the script header.
- Unit tests under `tests/` pass with `uv run pytest`.

## Additional Context

### Dependencies

- **`heyma-gateway`** at `ws://192.168.1.12:<port>/v1/voice` (lives in 33GOD, separate spec). HeyMa depends on the wire contract above being honored. A fake server lives in tests for offline development.
- **`hey_tonny.tflite`** trained model (training is its own task, see `hey_tonny_training/TRAINING_GUIDE.txt`).
- **ReSpeaker HAT** or USB mic. Hardware failure on the HAT is documented in `chip-failure.md`; USB-mic fallback is a config flip.

### Testing Strategy

**Unit (fast, mocked):**

- pytest-asyncio everywhere. Mocks for sounddevice (numpy frame fixtures), openwakeword (stub Model), websockets (in-process fake server using `websockets.serve`). Each module has independent tests at its seam.

**Integration (offline):**

- `tests/test_main_smoke.py` boots the full main coroutine against a fake mic + fake gateway. Asserts a complete wake → stream → playback cycle and a clean shutdown.

**On-device smoke:**

- `satellite/scripts/deploy.sh` deploys, reloads, tails logs. Manual: speak the wake word + a short utterance with the real gateway (or a stub gateway running on the dev box). Confirm wake log, PCM stream observed in gateway logs, WAV played through speaker.
- Failure drills: stop the gateway service, trigger wake, expect structured backoff logs and no crash. Pull the network cable, expect graceful degradation.

### Notes

- All `heyma-satellite/` modules expose narrow seams (one Protocol-typed class each) so the four components can be unit-tested without each other.
- Structured logging via stdlib `logging` with JSON formatter, fields: `event`, `session_id`, `latency_ms`, `gateway_url`. Hooks for shipping to the central log aggregator are out of scope here, deferred until observability story lands.
- `heyma-gateway` port and path are config, not hardcoded. Default placeholder until the gateway team commits a port: `ws://192.168.1.12:8778/v1/voice`.
