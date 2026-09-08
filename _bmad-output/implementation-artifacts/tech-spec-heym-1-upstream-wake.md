---
title: 'HEYM-1: Upstream wake detection'
slug: 'heym-1-upstream-wake'
created: '2026-09-08'
status: 'in-progress'
baseline_commit: '5a4b3e6'
ticket: 'HEYM-1'
supersedes: 'tech-spec-d4-wake.md'
---

# Tech Spec: HEYM-1 Upstream Wake Detection

## Intent

Restore hands-free "Hey Tonny" operation while keeping all ML inference off the Raspberry Pi. Tonny continuously relays validated 16 kHz mono PCM to the voice gateway on `big-chungus.local`. The gateway runs the deployed `hey_tonny.onnx` classifier, captures the request after detection, executes the existing STT, LLM, and TTS turn, then returns a WAV response to Tonny for ALSA playback.

## Evidence and constraints

- The current Python satellite is healthy but push-to-talk only. It waits for `SIGUSR1`.
- A forced turn proved Tonny capture, gateway STT/LLM/TTS, response delivery, and ALSA playback complete successfully.
- The project rule forbids ML inference on the Pi. The earlier D4 tract design is therefore blocked and must not be deployed.
- The exact deployed custom classifier was recovered from Tonny. SHA-256: `558bd199797084e41f6e1e9fd3cd330fb9920af5d55e06d2c647659bab33a5a0`.
- The classifier accepts `[1,16,96]` embeddings and scored `0.8534448` on a fresh synthetic "Hey Tonny" sample with openWakeWord 0.6.0 on macOS.
- Existing v0.1 push-to-talk clients must remain compatible.
- Configuration must use `big-chungus.local`, not a hard-coded LAN address.
- Network interruption must cause bounded retry with no replay of a submitted request.

## Protocol

- Add optional `mode` to `hello`: `turn` by default, or `continuous` for hands-free streaming.
- In continuous mode the client sends bounded, whole-sample PCM frames until the gateway returns a final response or closes the session.
- The gateway sends `wake_detected` with model name and finite score after the threshold is crossed.
- The gateway keeps a bounded pre-roll buffer and then collects a fixed post-wake window before running the existing voice pipeline.
- The gateway drains incoming PCM while providers run so a continuously writing client does not cancel the turn. A disconnect still cancels provider work.
- The connection closes after `response_end`; the satellite stops capture, plays the WAV, then reconnects for the next wake word.

## Code map

- `apps/voice/src/tonny_voice/wake.py`: server-side openWakeWord adapter and bounded detector state.
- `apps/voice/src/tonny_voice/app.py`: compatible continuous protocol path and wake observability.
- `apps/voice/src/tonny_voice/config.py`: wake model and capture bounds.
- `apps/voice/src/tonny_voice/assets/`: custom classifier plus feature-extractor ONNX assets.
- `apps/voice/pyproject.toml`, `apps/voice/uv.lock`: pinned server-only inference dependencies.
- `apps/satellite/satellite.py`: continuous ALSA capture, duplex WebSocket handling, reconnect, and playback.
- `deploy/tonny/satellite.service.in`: start the satellite in hands-free mode.
- `deploy/tonny/manage.py`, `deploy/tonny/Dockerfile.voice`: ship the model assets.
- Tests under `apps/voice/tests/` and `apps/satellite/tests/`: protocol, detector, timeout, retry, and backward compatibility.

## Tasks and acceptance

- [x] Add server-side custom-model inference and fail startup when the configured model is invalid.
- [x] Extend the protocol without breaking v0.1 turn mode.
- [x] Add continuous, bounded PCM capture to the Python satellite.
- [x] Keep inference dependencies and model execution off Tonny.
- [x] Verify unit and integration suites are green.
- [x] Verify the exact custom model detects a fresh "Hey Tonny" sample at score at least 0.5.
- [ ] Deploy through the existing snapshot-and-rollback workflow.
- [ ] Verify Tonny logs hands-free readiness and big-chungus health reports the deployed revision.
- [ ] Verify a spoken wake phrase produces `wake_detected`, one completed gateway response, and `playback_complete`.
- [x] Verify 30 seconds of silence produces no wake event.
- [ ] Re-record ONYX Input 2 during known Tonny playback. If it remains digital zero, stop software changes and correct the analog cable path.

## Rollback

The deployment manager snapshots both gateway and satellite units before activation. On any failed health or live check, restore the prior `tonny-voice.service`, `tonny-satellite.service`, and release. Push-to-talk remains available in the Python client and is the safe fallback.

## Implementation notes

- `openwakeword==0.6.0` is pinned on the Python 3.12 gateway. Its unused Linux-only TFLite runtime dependency is overridden because no CPython 3.12 wheel exists; ONNX Runtime remains locked and is the only inference backend selected by `wake.py`.
- The deterministic `say` fixture is PCM16 mono at 16 kHz with SHA-256 `5b187dfb003e18aa32e425ba4baf46c9b5537fced348251db752b40175172d90`.
- Local verification: 63 gateway tests passed with 1 browser test skipped; 28 satellite tests passed. Deployment and hardware acceptance tasks remain unchecked until they are proven live.
