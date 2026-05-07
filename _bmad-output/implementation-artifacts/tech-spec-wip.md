---
title: 'HeyMa Voice Satellite'
slug: 'heyma-satellite'
created: '2026-03-25'
updated: '2026-05-05'
status: 'done'
stepsCompleted: [1, 2, 3, 4, 5]
approved_at: '2026-05-05'
approved_by: 'Jarad'
baseline_commit: '6a54368a5f141747a4457d215a270f273b53a287'
layout_resolution: 'single-crate at heyma-satellite/ (resolves Tasks-vs-frontmatter inconsistency)'
hardware: 'Raspberry Pi Zero 2 W Rev 1.0 (aarch64, ARM Cortex-A53, 416 MB RAM, Debian 13 trixie)'
tech_stack: ['rust', 'tokio', 'cpal', 'tokio-tungstenite', 'oww-rs', 'tract-onnx (transitive via oww-rs)', 'serde', 'figment', 'tracing', 'systemd', 'async-trait', 'hound', 'futures-util']
files_to_modify:
  - 'heyma-satellite/' (new Cargo workspace)
  - 'heyma-satellite/Cargo.toml' (new, workspace root)
  - 'heyma-satellite/src/' (new, package source)
  - 'satellite/systemd/heyma.service' (new)
  - 'satellite/scripts/deploy.sh' (new, cross-compile + rsync binary)
  - 'satellite/scripts/decommission-wyoming.sh' (new)
  - 'docs/wire-contract.md' (new)
  - 'custom_wakewords/' (existing, holds the ONNX-exported hey_tonny model)
  - 'tonny.local: disable wyoming-satellite + wyoming-openwakeword units' (decommission)
code_patterns:
  - 'tokio multi-task supervisor with mpsc channels between mic, wake, gateway, and speaker'
  - 'cpal InputStream callback pushing PCM frames into an mpsc::Sender<AudioFrame>'
  - 'oww-rs Detector running on rolling 80 ms windows from the mpsc rx side'
  - 'tokio-tungstenite client with exponential backoff via tokio::time::sleep'
  - 'figment::Figment merging Env::prefixed("HEYMA_") + sane defaults into a Settings struct'
  - 'tracing with json subscriber, fields: event, session_id, latency_ms, gateway_url'
test_patterns:
  - 'tokio::test with in-process tokio_tungstenite::accept_async fake gateway'
  - 'oww-rs Detector stubbed with a trait so a sentinel-PCM fixture fires wake'
  - 'cpal mocked behind an AudioSource trait; tests feed pre-canned f32 frames'
  - 'cargo test for unit + integration; ssh smoke harness for on-device verification'
---

# Tech-Spec: HeyMa Voice Satellite

**Updated:** 2026-05-05 (gutted from prior multi-service design, then switched to Rust after hardware verification)

## Overview

### Problem Statement

The Pi Zero 2 W needs a single, focused job: be a dumb voice satellite. Wake on "hey tonny", stream mic audio to a single upstream endpoint, play whatever WAV comes back. The previous spec accreted Wyoming-protocol plumbing, server-side STT shims, Bloodbank routing, and TTS bridges that do not belong on (or near) this hardware. Every layer of that stack is upstream's concern.

### Solution

A single Rust binary, `heyma`, runs as one systemd unit on `tonny.local`. It uses:

- `oww-rs` to detect "hey tonny" locally on a rolling audio buffer using the ONNX-exported wake-word model.
- `cpal` for mic capture and speaker playback against the ReSpeaker HAT (or USB mic fallback).
- `tokio-tungstenite` to connect to `heyma-gateway` at `ws://192.168.1.12:<port>/v1/voice`, stream raw PCM during an utterance, and play the binary WAV the gateway returns.

That replaces `wyoming-satellite`, `wyoming-openwakeword`, and all proposed server-side custom code from this repo. Everything STT/agent/TTS lives behind the `heyma-gateway` boundary.

### Scope

**In Scope:**

- Pi-side audio capture, wake detection, end-of-utterance detection, transport, and playback.
- WebSocket wire contract documentation (`docs/wire-contract.md`).
- systemd unit, cross-compile + deploy script, and decommission of the existing Wyoming units.
- Unit tests for the four module seams (audio, wake, gateway, config) plus an integration smoke test.

**Out of Scope:**

- `heyma-gateway` implementation (separate repo, separate spec).
- STT engine, agent orchestration, TTS engine selection.
- Wake word model training (`hey_tonny_training/` already covers it). One added requirement: training output must include the ONNX export, not just `.tflite`.
- Audio observability, distributed tracing, structured log shipping (deferred).

## Context for Development

### Verified Hardware (2026-05-05 via `ssh tonny.local`)

- Board: Raspberry Pi Zero 2 W Rev 1.0
- Arch: `aarch64` (ARMv8, Cortex-A53 quad-core, `CPU part 0xd03`)
- OS: Debian 13 (trixie), kernel `6.12.62+rpt-rpi-v8`
- RAM: 416 MB total, 213 MB available, 415 MB swap (29 MB used)

This unlocks the Rust path: ONNX Runtime ships prebuilt aarch64 binaries, `oww-rs` works without source builds, `cpal` and `tokio-tungstenite` cross-compile cleanly to `aarch64-unknown-linux-gnu`. The 416 MB RAM ceiling makes Rust's ~10 MB resident footprint a real win over a Python interpreter at ~80-150 MB.

### Assumptions (correct me before I proceed)

1. `heyma-gateway` will be built in parallel and conform to the wire contract below. The satellite can be developed and unit-tested against a fake gateway in `tokio::test` before the real one ships.
2. The trained wake-word model is exported as ONNX (`hey_tonny.onnx`) and lands at `/home/delorenj/custom_wakewords/hey_tonny.onnx` on the Pi. The `hey_tonny_training/TRAINING_GUIDE.txt` workflow already supports the openwakeword Colab pipeline; the only addition is invoking the ONNX export step at the end.
3. Audio capture device is selectable by name via env var (`HEYMA_MIC_DEVICE`). ReSpeaker HAT or USB mic both viable depending on the hardware-failure status documented in `chip-failure.md`.
4. End-of-utterance is detected by simple RMS silence threshold + min/max utterance guards. The `webrtc-vad` crate is the upgrade path if accuracy is insufficient.
5. Cross-compilation happens on `big-chungus` (the dev box) targeting `aarch64-unknown-linux-gnu`. Deploy is `rsync` of the static binary plus the systemd unit.

### Codebase Patterns

- Resource ceiling: 416 MB RAM. Aim for under 30 MB resident.
- Network is unreliable. The client must reconnect with exponential backoff and never crash the systemd unit on transient failure. `tokio::select!` between work and a shutdown signal so unit restart is clean.
- Audio device must be released cleanly on every shutdown (`Drop` on the cpal `Stream` plus an explicit `drop` in shutdown paths).
- All config via `HEYMA_*` env vars, hydrated through `figment` into a single `Settings` struct.
- Structured logs via `tracing` with the `tracing-subscriber` JSON formatter. Required fields on every wake/stream event: `event`, `session_id`, `latency_ms`, `gateway_url`.

### Files to Reference

| File                                                           | Purpose                                                                                        |
| -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `rec.py`                                                       | Working sounddevice ReSpeaker capture pattern, mine for the device-name string and rate config |
| `hey_tonny_training/TRAINING_GUIDE.txt`                        | Wake word model production workflow; add ONNX export step                                      |
| `tonny.local:/etc/systemd/system/wyoming-satellite.service`    | Existing unit to be stopped + disabled                                                         |
| `tonny.local:/etc/systemd/system/wyoming-openwakeword.service` | Existing unit to be stopped + disabled                                                         |

### Technical Decisions

- **WebSocket bidirectional over HTTP request/response:** lower latency (gateway can begin synthesis before the user finishes speaking), one persistent connection with clear lifecycle, single endpoint to monitor.
- **No Wyoming on the Pi:** the Wyoming protocol's value is multi-service routing (ASR, intent, TTS). Since the gateway collapses all of that behind one WS endpoint, Wyoming's surface area is pure overhead.
- **Rust over Python:** verified aarch64 hardware enables ONNX Runtime aarch64 prebuilts, which removes the original "ARMv6 makes Rust impractical" concern. With 416 MB total RAM, the ~10 MB binary versus ~100 MB interpreter delta is meaningful headroom. No GC pauses for audio realtime. Single static deploy artifact, no `uv sync` step on the Pi.
- **`oww-rs` (or vendored equivalent):** runs openwakeword's actual ONNX-exported models, so the existing training workflow stays. If `oww-rs` isn't on crates.io at the right version, vendor the source under `vendor/oww-rs` and pin the SHA.
- **`cpal` over `alsa-rs`:** portable, idiomatic, and wraps ALSA on Linux. The deeper ALSA bindings would only be necessary if cpal's latency proves insufficient.
- **`heyma-gateway` as named peer:** the wire contract is the only thing HeyMa knows about it. The gateway can be replaced or rearchitected freely without Pi changes.

## Implementation Plan

### Tasks

**Phase 1 - Cargo workspace + core crate**

- [x] `heyma-satellite/Cargo.toml` -- workspace root. One member: `heyma`. Pinned aarch64 cross-compile target documented in workspace metadata.
- [x] `heyma-satellite/heyma/Cargo.toml` -- deps: `tokio` (rt-multi-thread, macros, sync, time, signal), `tokio-tungstenite`, `cpal`, `oww-rs` (or vendored), `ort`, `serde`, `serde_json`, `figment` (env, toml), `tracing`, `tracing-subscriber` (json, env-filter), `uuid` (v4), `bytes`, `anyhow`, `thiserror`. Dev-deps: `tokio-test`, `assert_matches`, `tempfile`.
- [x] `heyma-satellite/heyma/src/config.rs` -- `Settings` struct: `gateway_url`, `wake_model_path`, `wake_threshold`, `mic_device`, `speaker_device`, `sample_rate` (default 16000), `silence_threshold_db`, `min_utterance_ms`, `max_utterance_ms`. Loaded via `Figment::new().merge(Env::prefixed("HEYMA_"))`.
- [x] `heyma-satellite/heyma/src/audio.rs` -- `AudioSource` trait + `CpalAudioSource` impl that owns the cpal InputStream and pushes 80 ms PCM frames into an `mpsc::Sender<AudioFrame>`. `AudioSink` trait + `CpalAudioSink` impl that plays a WAV byte slice via cpal output stream.
- [x] `heyma-satellite/heyma/src/wake.rs` -- `WakeDetector` trait + `OwwDetector` impl wrapping `oww-rs`. `async fn run(rx: mpsc::Receiver<AudioFrame>) -> mpsc::Receiver<WakeEvent>`.
- [x] `heyma-satellite/heyma/src/utterance.rs` -- RMS-based silence detector with `min_utterance_ms` floor and `max_utterance_ms` ceiling. Yields `EndOfInput` when sustained silence is observed.
- [x] `heyma-satellite/heyma/src/gateway.rs` -- `GatewayClient`: connect via tokio-tungstenite, send `hello`, await `ready`, stream PCM as binary frames, send `end_of_input`, await `response_start` + WAV bytes + `response_end`. Exponential backoff on transport errors (1s → 30s cap).
- [x] `heyma-satellite/heyma/src/main.rs` -- supervisor task wiring everything. `tokio::select!` over wake events, gateway lifecycle, and a SIGTERM watcher. Per-utterance state machine: idle → listening → streaming → playing → idle.

**Phase 2 - Pi deployment**

- [x] `satellite/systemd/heyma.service` -- `Type=simple`, `Restart=on-failure`, `RestartSec=5`, `ExecStart=/usr/local/bin/heyma`, `EnvironmentFile=/etc/heyma.env`. `WorkingDirectory=/home/delorenj`. Audio access via the `audio` group.
- [x] `satellite/scripts/deploy.sh` -- idempotent: `cargo build --release --target aarch64-unknown-linux-gnu` on big-chungus (using `cross` if needed), `rsync` binary to `tonny.local:/usr/local/bin/heyma`, `scp` unit file, `ssh tonny.local 'systemctl daemon-reload && systemctl restart heyma && journalctl -u heyma -f'`.
- [x] `satellite/scripts/decommission-wyoming.sh` -- `ssh tonny.local 'systemctl stop wyoming-satellite wyoming-openwakeword && systemctl disable wyoming-satellite wyoming-openwakeword'`. Reversible: keeps unit files in place. Header comments document re-enable command.

**Phase 3 - Wire contract + tests**

- [x] `docs/wire-contract.md` -- complete WS protocol doc with message-trace Mermaid diagram (mirrors the Wire Contract section below).
- [x] `heyma-satellite/heyma/tests/config.rs` -- env-var hydration, default-fallback, validation errors.
- [x] `heyma-satellite/heyma/tests/wake.rs` -- stub `WakeDetector` impl fires on a sentinel PCM fixture; assert no false-positives on silence.
- [x] `heyma-satellite/heyma/tests/utterance.rs` -- silence detection on synthetic sine + zero buffer; min/max guards.
- [x] `heyma-satellite/heyma/tests/gateway.rs` -- in-process `tokio_tungstenite::accept_async` fake gateway; assert hello + binary frames + `end_of_input` + WAV consumed.
- [x] `heyma-satellite/heyma/tests/smoke.rs` -- end-to-end with stub mic source, stub speaker sink, fake gateway. One full wake → stream → playback cycle.

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

- Given the unit is running and `hey_tonny.onnx` is loaded, when "hey tonny" is spoken, then `journalctl -u heyma` logs a `wake_detected` event within 500 ms of utterance end.
- Given a wake event, when the user keeps speaking and then stops, then the client opens a WS to the configured gateway, streams PCM frames, and sends `end_of_input` within 250 ms of detected silence.
- Given the gateway returns `response_start` + WAV + `response_end`, when the WAV is received, then it plays through the configured speaker device with no truncation.
- Given the gateway is unreachable when wake fires, when the client retries, then it backs off (1s, 2s, 4s, 8s, capped at 30s) and never crashes the systemd unit. Logs structured `gateway_unreachable` events with `attempt` and `next_delay_ms` fields.
- Given the gateway disconnects mid-utterance, when the client detects the closed socket, then it aborts the current utterance, drops audio buffers, returns to wake-listening, and reconnects on next wake.
- Given `decommission-wyoming.sh` has run, when `ssh tonny.local 'systemctl status wyoming-satellite'` is invoked, then the unit is `inactive (dead)` and `disabled`. Reversal documented in the script header.
- `cargo test` passes on `big-chungus` (host target) with all suites green. Cross-compiled binary boots cleanly on `tonny.local` and `journalctl -u heyma` shows `service_ready` within 2 s of `systemctl start heyma`.

## Additional Context

### Dependencies

- **`heyma-gateway`** at `ws://192.168.1.12:<port>/v1/voice` (lives in 33GOD, separate spec). HeyMa depends on the wire contract above being honored. The fake gateway in tests is the offline-dev surface.
- **`hey_tonny.onnx`** trained model (training is its own task, see `hey_tonny_training/TRAINING_GUIDE.txt`). The training task gets one new step: ONNX export.
- **ReSpeaker HAT** or USB mic. Hardware failure on the HAT is documented in `chip-failure.md`. Switching is a `HEYMA_MIC_DEVICE` env-var flip.
- **`cross` toolchain** on big-chungus for cross-compilation if the native rustc target setup is finicky. Documented in `satellite/scripts/deploy.sh`.

### Testing Strategy

**Unit (fast, mocked):**

- `tokio::test` everywhere. Trait seams (`AudioSource`, `AudioSink`, `WakeDetector`, `GatewayClient`) let each module be tested without the others. cpal is mocked behind `AudioSource`. The gateway tests use `tokio_tungstenite::accept_async` on a localhost socket as a real-protocol fake.

**Integration (offline, host target):**

- `tests/smoke.rs` runs the full supervisor against a stub mic source, stub speaker sink, and a fake gateway. Asserts a complete wake → stream → playback cycle and a clean shutdown on simulated SIGTERM.

**On-device smoke:**

- `satellite/scripts/deploy.sh` cross-compiles, rsyncs the binary, restarts the unit, tails logs. Manual: speak the wake word + a short utterance with the real gateway (or a stub gateway running on big-chungus). Confirm `wake_detected` log, PCM stream observed in gateway logs, WAV played through speaker.
- Failure drills (executed via `ssh tonny.local`): stop the gateway service, trigger wake, expect structured backoff logs and no crash. Disable wifi, expect graceful degradation and clean reconnect on restore.

### Notes

- The four trait seams are the architectural commitment. Concrete types (`CpalAudioSource`, `OwwDetector`, `TungsteniteGateway`) are swappable without touching `main.rs`.
- `tracing` events use `event=...` as a structured field (not a span name) so JSON parsing on the gateway side is uniform.
- `heyma-gateway` port and path are config, not hardcoded. Default placeholder until the gateway team commits a port: `ws://192.168.1.12:8778/v1/voice`.
- The project CLAUDE.md still says "Pi Zero W". A follow-up task should update it to "Pi Zero 2 W (aarch64)" once this spec ships.

## Suggested Review Order

**Architecture entry point**

- Per-utterance gateway factory replaces shared mutex; the design intent of the whole crate.
  [`main.rs:28`](../../heyma-satellite/src/main.rs#L28)

- Factory wiring at process startup and signal handling.
  [`main.rs:317`](../../heyma-satellite/src/main.rs#L317)

**Wire contract**

- Send/receive state machine: hello, ready, PCM stream, end_of_input, response WAV, response_end.
  [`gateway.rs:135`](../../heyma-satellite/src/gateway.rs#L135)

- Connect-with-timeout + bounded retry deadline.
  [`gateway.rs:121`](../../heyma-satellite/src/gateway.rs#L121)

- Backoff and `gateway_unreachable` log with all four required structured fields.
  [`gateway.rs:161`](../../heyma-satellite/src/gateway.rs#L161)

- 10 MB cap on WAV buffer to prevent OOM on a 416 MB Pi.
  [`gateway.rs:78`](../../heyma-satellite/src/gateway.rs#L78)

- Protocol doc the implementation must match exactly.
  [`wire-contract.md`](../../docs/wire-contract.md)

**Audio realtime safety**

- Trait seams pinned at `Send + 'static`; concrete impls live below.
  [`audio.rs:54`](../../heyma-satellite/src/audio.rs#L54)

- USB-unplug detection: err_fn flips an atomic, parking loop drops the sender.
  [`audio.rs:113`](../../heyma-satellite/src/audio.rs#L113)

- Playback spin-wait now has a 30s ceiling so a stuck output stream cannot freeze the device.
  [`audio.rs:182`](../../heyma-satellite/src/audio.rs#L182)

**Wake detection**

- Init failure now propagates an error instead of silently closing the channel.
  [`wake.rs:71`](../../heyma-satellite/src/wake.rs#L71)

- Inner detector log renamed to `wake_score_above_threshold` to disambiguate from supervisor's outer log.
  [`wake.rs:115`](../../heyma-satellite/src/wake.rs#L115)

**Config fail-fast**

- URL scheme validation, sample-rate enforcement, and the new `gateway_connect_deadline_ms` knob.
  [`config.rs:118`](../../heyma-satellite/src/config.rs#L118)

**Failure mode coverage**

- New test exercises mid-utterance gateway disconnect (AC #5).
  [`tests/gateway.rs:374`](../../heyma-satellite/tests/gateway.rs#L374)

**Deployment**

- systemd unit hardened: `Restart=always`, longer rate-limit window.
  [`heyma.service`](../../satellite/systemd/heyma.service)

- Deploy script: cross-compile, binary-size guard, optional `--tail` for log follow.
  [`deploy.sh`](../../satellite/scripts/deploy.sh)

- Reversible decommission of legacy Wyoming units.
  [`decommission-wyoming.sh`](../../satellite/scripts/decommission-wyoming.sh)

**Known limitations (do not address in this spec)**

- D1 self-wake echo, D2 silent-mic watchdog, D3 `play_wav` still blocks supervisor during playback.
  [`deferred-work.md`](deferred-work.md)

