---
title: HeyMa Voice Satellite — Deferred Work
created: '2026-05-05'
source: 'three-way review of tech-spec-wip.md (blind hunter + edge case hunter + acceptance auditor)'
---

# Deferred Work

These findings are real but not blockers for v0.1. They were surfaced during the Step 4 adversarial review and consciously deferred. Each is a candidate for a future spec.

## D1 — Self-wake from speaker echo

**What:** While the satellite is playing a TTS response, the mic is still active and feeding into `wake_tx`. If the response audio contains the wake-word phrase ("hey tonny"), the speaker's own playback can be picked up by the mic and re-trigger the wake detector.

**Why deferred:** Low real-world likelihood (TTS responses rarely include the literal wake phrase). Mitigation requires either (a) acoustic echo cancellation, which is non-trivial DSP work, or (b) muting/gating the mic during playback, which is simpler but introduces "deaf during response" UX.

**Suggested v0.2 approach:** Gate `wake_tx.send` while `active_session.is_some()` AND `playing == true`. One boolean flag flipped in the wav_rx supervisor arm. ~5 lines of code, no DSP.

## D2 — Silent-mic detection / heartbeat

**What:** No watchdog detects the case where the mic is connected and the cpal stream is "running" but produces only zero samples (mute switch on, broken codec). The satellite sits idle indefinitely without firing wake events; from systemd's perspective it's healthy.

**Why deferred:** Operationally rare (user would notice "Tonny isn't responding"), and the fix is in the observability story, not the v0.1 wire-contract story.

**Suggested v0.2 approach:** Periodic RMS sample of incoming frames. If 60+ seconds of near-zero RMS pass with no wake event, log `audio_silent_for_minutes` at WARN. Optional: surface as a metric for an external watchdog.

## D3 — `play_wav` still blocks supervisor (F5 partial fix)

**What:** F5 from the Step 4 review was classified as `patch` and instructed: "wrap `play_wav` in `tokio::task::spawn_blocking` so it doesn't block the runtime." The applied fix added the 30s spin-wait timeout inside `CpalAudioSink::play_wav` (good — prevents indefinite hang on a stuck output stream), but did NOT actually offload `play_wav` to a blocking thread. The `spawn_blocking` in `main.rs` only shuttles the WAV bytes; `sink.play_wav(...)` still runs on the supervisor's async task and blocks the `select!` loop for the full response duration.

**Why deferred:** The proper fix requires a trait-signature change. `AudioSink::play_wav(&mut self, ...)` cannot be moved into `spawn_blocking` without either (a) wrapping the sink in `Arc<Mutex<Self>>` and cloning the Arc per utterance, or (b) changing the signature to `play_wav(self: Arc<Self>, ...) -> impl Future`, or (c) making `AudioSink: Clone`. Each option touches the architectural commitment ("4 trait seams") and deserves a fresh spec round.

**Real-world impact today:** Wakes that fire while the satellite is playing a TTS response are queued and processed after playback completes. Worst-case delay is bounded by `max_utterance_ms` (default 30s) and the gateway's response duration. The system never hangs indefinitely (30s spin-wait timeout is the safety net). UX cost: barge-in is impossible, and rapid back-to-back utterances are serialized.

**Suggested v0.2 approach:** Either wrap `CpalAudioSink` in `Arc<Mutex<...>>` at construction and clone into `spawn_blocking`, or refactor `AudioSink` to `play_wav(self: Arc<Self>, wav: Bytes) -> Pin<Box<dyn Future<Output = Result<()>> + Send>>`. Both unblock the supervisor during playback.

## D4 — Real wake detection (`real-wake` feature) does not compile against oww-rs 0.2.0

**What:** rust-pro's `OwwDetector` in `heyma-satellite/src/wake.rs` (gated behind the `real-wake` Cargo feature) imports `oww_rs::Detector` and calls `Detector::new(model_path, threshold)`. That API does not exist in `oww-rs 0.2.0`. The actual public surface of the crate exposes only `create_unlock_task_sync(running: CancellationToken, chunks_sender: broadcast::Sender<ChunkType>) -> Result<bool, String>`, which owns its own cpal-based mic loop. The `Model` trait, `Models::new`, and `model::new_model` are all `pub(crate)`, so we cannot drive the inference engine from our own audio pipeline.

**Why deferred:** Discovered during the cross-compile dry-run. The host test suite missed it because tests don't enable the `real-wake` feature. Fixing this requires either (a) rewriting `wake.rs` to integrate with `create_unlock_task_sync`'s broadcast-channel model (and likely changing our `AudioSource` architecture to coexist with oww-rs's mic ownership), (b) vendoring + patching oww-rs to expose a low-level API, or (c) switching to a different wake-detector crate entirely (Rustpotter, livekit-wakeword, or rolling our own with `tract-onnx` against openwakeword's ONNX models directly).

**Real-world impact today:** The shipped binary uses `StubWakeDetector` regardless of intent. Stub fires only on a hardcoded sentinel sample value not present in real audio, so the satellite will never wake on speech. The system is functional for testing transport / wire-contract / playback paths, but cannot detect "hey tonny" until D4 is resolved. `deploy.sh` no longer passes `--features real-wake` to keep the build from breaking; the flag stays defined in `Cargo.toml` for the eventual fix.

**Suggested v0.2 approach:** Most direct path is option (c) — bypass `oww-rs` entirely. `tract-onnx` is the inference engine `oww-rs` itself uses; we can load openwakeword's published ONNX models (`embedding_model.onnx` + the wake-word-specific classifier) ourselves and run a 2-stage inference pipeline against PCM frames from our existing `AudioSource`. ~150-300 lines of Rust, no fork debt, full control over the threading model.

## Notes

- D1 and D2 came from the edge-case hunter review.
- D3 is a patch that turned out to need a spec amendment we didn't do; flagged honestly here rather than claiming false coverage.
- D4 was discovered during the cross-compile dry-run after the spec was already approved. Pretty significant for shipping a working wake-on-voice device, since without it the binary literally can't hear "hey tonny." But it's deferrable because everything downstream of wake (transport, playback, reconnect, observability) is already correct and testable end-to-end via fakes.
- None of these block the wire contract or the trait seam architecture.
- Do not implement any of these without a fresh spec round; all four touch architectural surface that wasn't designed for them in v0.1.
