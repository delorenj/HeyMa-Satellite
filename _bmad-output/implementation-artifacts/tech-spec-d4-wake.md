---
title: 'D4 — Real Wake Detection via tract-onnx'
slug: 'd4-wake-detection'
parent: 'tech-spec-wip.md'
created: '2026-05-11'
status: 'in-progress'
baseline_commit: '5eedf93'
---

# Tech-Spec: D4 — Real Wake Detection

## Intent

**Problem:** The current `OwwDetector` (real-wake feature) imports `oww_rs::Detector` which does not exist in `oww-rs 0.2.0`. The crate's only public entry is `create_unlock_task_sync`, which owns its own cpal mic loop and cannot be driven from our `AudioSource` trait. The deployed binary uses `StubWakeDetector` and cannot wake on speech.

**Approach:** Bypass `oww-rs` entirely. Run openwakeword's published 3-stage ONNX pipeline (melspectrogram → embedding → classifier) directly against `tract-onnx`, fed from our existing `AudioSource`. Universal models (mel + embedding) bundle into the binary via `include_bytes!`. The per-wake-word classifier loads at runtime from `HEYMA_WAKE_MODEL_PATH`.

## Pipeline (from oww-rs source audit)

| Stage | Input | Output | Source |
|---|---|---|---|
| Mel-spectrogram | `[1, 1280]` f32 (80 ms PCM at 16 kHz) | `[5, 32]` f32 | `melspectrogram.onnx` |
| Embedding | `[1, 76, 32, 1]` f32 (stacked mels) | `[1, 1, 1, 96]` f32 | `embedding_model.onnx` |
| Classifier | `[1, 16, 96]` f32 (stacked embeddings) | `[1, 1]` f32 score | `<wake_word>.onnx` |

**Buffers (all FIFO/circular):**
- Mel buffer: 16 slots of `[5, 32]` mel frames. On each new mel, push and produce stacked `[76, 32]` by concatenating last 16 frames and slicing the time axis.
- Embedding buffer: 16 slots of `[96]` vectors. On each new embedding, push and produce stacked `[16, 96]`.
- Detection probability buffer: 12 slots of f32 scores for the moving-average detection logic.

**Detection logic (mirrors oww-rs):**
- Frame-level rejection: score < 0.1 → ignored.
- Moving average: sum of supra-threshold (>0.1) scores divided by their count. Must exceed `wake_threshold` (default 0.5).
- Peak gate: the **current** frame's raw score must drop **below 0.1** while the moving average is above threshold. This is the "trailing edge" detection that fires once per utterance instead of repeatedly.
- Debounce: 2000 ms minimum between consecutive detections.

**Audio normalization:**
- i16 → f32: divide by 32768.0.
- After mel inference: `v = v / 10.0 + 2.0` (oww-rs convention; the openwakeword melspectrogram model expects this).

## Boundaries & Constraints

**Always:**
- Mel + embedding ONNX bytes are bundled at compile time via `include_bytes!("../assets/openwakeword/melspectrogram.onnx")` etc. No runtime fetch.
- Classifier ONNX loaded at startup from `Settings.wake_model_path`. Init failure is fatal (propagate via existing wake-error path; supervisor exits non-zero, systemd restarts).
- Preserve the `WakeDetector` trait seam and the `feature = "real-wake"` gate. Default build still uses `StubWakeDetector`.
- All inference runs in a single tokio task spawned by `OwwDetector::start`, consuming `mpsc::Receiver<AudioFrame>` and producing `mpsc::Sender<WakeEvent>`. No new async surface.

**Ask First:** None (delegated to autopilot per Otto run; only stop on concrete blockers).

**Never:**
- Do NOT add oww-rs back as a dep. Drop it from Cargo.toml.
- Do NOT spawn additional cpal streams; we consume frames from the existing `AudioSource`.
- Do NOT change the wire contract, `Settings`, or any other module.

## Code Map

- `heyma-satellite/Cargo.toml` — drop `oww-rs`, add direct deps `tract-onnx = "0.22"` (with default features) and `ndarray = "0.16"` for tensor manipulation. Replace `[features] real-wake = ["oww-rs"]` with `real-wake = ["dep:tract-onnx", "dep:ndarray"]`.
- `heyma-satellite/assets/openwakeword/{melspectrogram,embedding_model,alexa}.onnx` — staged binary assets.
- `heyma-satellite/src/wake.rs` — rewritten `OwwDetector` against `tract-onnx`. Stub kept untouched.
- `heyma-satellite/tests/wake.rs` — add `test_real_detector_loads_models` (gated `#[cfg(feature = "real-wake")]`) and a positive-detection test using a real WAV fixture.
- `heyma-satellite/tests/fixtures/alexa.wav` — short WAV containing "alexa" speech (sourced from oww-rs's test fixtures if available, else recorded ad hoc).

## Tasks & Acceptance

**Execution:**
- [ ] `Cargo.toml` — Drop `oww-rs`, add `tract-onnx` + `ndarray`, update feature gate.
- [ ] `src/wake.rs` — Implement the 3-stage pipeline + buffer logic + detection rule with the same trait surface.
- [ ] `tests/wake.rs` — Add real-wake-gated tests covering: model load, mel-only smoke, full pipeline on known wake WAV, no false-positive on silence.
- [ ] Verify `cargo test` passes with both default features and `--features real-wake`.
- [ ] Verify `cross build --release --target aarch64-unknown-linux-gnu --features real-wake` produces a binary.
- [ ] Re-enable `--features real-wake` in `satellite/scripts/deploy.sh`.
- [ ] Deploy to `tonny.local` with stock `alexa.onnx`; verify saying "alexa" fires `wake_detected` in journalctl.

**Acceptance Criteria:**
- Given the real-wake binary is deployed on `tonny.local` with `HEYMA_WAKE_MODEL_PATH` pointing at a valid `alexa.onnx`, when the word "alexa" is spoken near the mic, then `journalctl -u heyma` emits a `wake_detected` event with `score >= 0.5` within 1 second.
- Given the same setup, when continuous silence is fed for 30 seconds, then no `wake_detected` events fire.
- Given an invalid or missing classifier path, when the unit starts, then the wake task emits `wake_init_failed` and the supervisor returns a non-zero exit code (systemd restarts).
- `cargo test --features real-wake` is green (all suites).
- The cross-compiled binary size stays under 10 MB (sanity bound; expected ~6 MB after bundling the 2.4 MB of mel + embedding ONNX).

## Design Notes

The oww-rs source was the reference. The implementation should follow its pipeline structure but use our own buffer types and trait wiring. Key fidelity points:
- The mel buffer's 16 slots × 5 mel-time-frames = 80 mel frames, sliced [4:80] to get the 76-frame input the embedding model expects.
- The embedding output's `[1, 1, 1, 96]` shape needs to be flattened to `[96]` before pushing into the embedding buffer; the classifier expects `[1, 16, 96]`.
- Single-frame raw scores below 0.1 reset the trailing-edge detection state, preventing the "wake fires repeatedly while user is still talking" failure mode.

## Verification

**Commands:**
- `cd heyma-satellite && cargo test` — expected: default suite green (41+ tests).
- `cd heyma-satellite && cargo test --features real-wake` — expected: real-wake suite green (model loads, detection fires on fixture).
- `cd heyma-satellite && cross build --release --target aarch64-unknown-linux-gnu --features real-wake` — expected: binary at `target/aarch64-unknown-linux-gnu/release/heyma`, ~6 MB.
- `bash satellite/scripts/deploy.sh` — expected: deploy lands cleanly, service starts.
- `ssh tonny.local 'sudo journalctl -u heyma -f'` then speak "alexa" — expected: `wake_detected` event in <1 s.
