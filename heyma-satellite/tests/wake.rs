// tests/wake.rs — WakeDetector trait + StubWakeDetector behavior.
// The real-wake block at the bottom is gated behind #[cfg(feature = "real-wake")].

#[path = "../src/audio.rs"]
mod audio;
#[path = "../src/config.rs"]
mod config;
#[path = "../src/wake.rs"]
mod wake;

use audio::AudioFrame;
use wake::{StubWakeDetector, WakeDetector, WAKE_SENTINEL};

/// Build a 80 ms PCM frame (2560 bytes at 16 kHz mono) filled with the given sample value.
fn filled_frame(sample: i16) -> AudioFrame {
    let samples = vec![sample; 1280]; // 16000 * 0.08 = 1280 samples
    AudioFrame::from_samples(&samples)
}

/// Build a silent frame (all zeros).
fn silent_frame() -> AudioFrame {
    filled_frame(0)
}

/// Build a wake-trigger frame (first sample = WAKE_SENTINEL).
fn wake_frame() -> AudioFrame {
    let mut samples = vec![0i16; 1280];
    samples[0] = WAKE_SENTINEL;
    AudioFrame::from_samples(&samples)
}

/// Build a non-sentinel, non-silent frame (e.g. a sine-ish signal).
fn signal_frame() -> AudioFrame {
    let samples: Vec<i16> = (0..1280)
        .map(|i| {
            // Simple sawtooth, never equals WAKE_SENTINEL
            let v = ((i % 100) as i32 * 200 - 10_000) as i16;
            // Avoid accidentally hitting WAKE_SENTINEL
            if v == WAKE_SENTINEL { v.wrapping_add(1) } else { v }
        })
        .collect();
    AudioFrame::from_samples(&samples)
}

#[tokio::test]
async fn test_stub_detector_fires_on_sentinel_frame() {
    let (tx, rx) = tokio::sync::mpsc::channel(8);
    let detector = Box::new(StubWakeDetector);
    let mut wake_rx = detector.start(rx);

    // Send a wake-trigger frame.
    tx.send(wake_frame()).await.unwrap();
    // Give the async task a moment to process.
    let event = tokio::time::timeout(
        std::time::Duration::from_millis(200),
        wake_rx.recv(),
    )
    .await
    .expect("timed out waiting for wake event")
    .expect("wake channel closed")
    .expect("wake detector failed");

    assert!(event.detected_at_ms > 0);
    assert_eq!(event.score, 1.0);
}

#[tokio::test]
async fn test_stub_detector_no_false_positive_on_silence() {
    let (tx, rx) = tokio::sync::mpsc::channel(32);
    let detector = Box::new(StubWakeDetector);
    let mut wake_rx = detector.start(rx);

    // Send 10 silent frames.
    for _ in 0..10 {
        tx.send(silent_frame()).await.unwrap();
    }
    // Allow brief processing window.
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;

    // wake_rx should have no pending events.
    assert!(wake_rx.try_recv().is_err(), "silent frames must not trigger wake");
}

#[tokio::test]
async fn test_stub_detector_no_false_positive_on_signal_noise() {
    let (tx, rx) = tokio::sync::mpsc::channel(32);
    let detector = Box::new(StubWakeDetector);
    let mut wake_rx = detector.start(rx);

    // Send 10 signal (non-sentinel) frames.
    for _ in 0..10 {
        tx.send(signal_frame()).await.unwrap();
    }
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;

    assert!(
        wake_rx.try_recv().is_err(),
        "non-sentinel signal frames must not trigger wake"
    );
}

#[tokio::test]
async fn test_stub_detector_fires_exactly_once_per_sentinel() {
    let (tx, rx) = tokio::sync::mpsc::channel(16);
    let detector = Box::new(StubWakeDetector);
    let mut wake_rx = detector.start(rx);

    // silent → wake → silent → wake → silent
    tx.send(silent_frame()).await.unwrap();
    tx.send(wake_frame()).await.unwrap();
    tx.send(silent_frame()).await.unwrap();
    tx.send(wake_frame()).await.unwrap();
    tx.send(silent_frame()).await.unwrap();
    drop(tx);

    // Collect all events.
    let mut count = 0;
    loop {
        match tokio::time::timeout(
            std::time::Duration::from_millis(200),
            wake_rx.recv(),
        )
        .await
        {
            Ok(Some(Ok(event))) => {
                count += 1;
                assert!(event.detected_at_ms > 0);
            }
            Ok(Some(Err(error))) => panic!("wake detector failed: {error}"),
            Ok(None) | Err(_) => break,
        }
    }
    assert_eq!(count, 2, "exactly 2 wake events expected for 2 sentinel frames");
}

#[tokio::test]
async fn test_stub_detector_stops_when_sender_dropped() {
    let (tx, rx) = tokio::sync::mpsc::channel(8);
    let detector = Box::new(StubWakeDetector);
    let mut wake_rx = detector.start(rx);

    // Drop sender immediately.
    drop(tx);

    // The wake_rx channel should close cleanly (recv returns None).
    let result = tokio::time::timeout(
        std::time::Duration::from_millis(200),
        wake_rx.recv(),
    )
    .await;
    let received = result.expect("wake detector did not stop after its input closed");
    assert!(
        received.is_none(),
        "unexpected wake output after input sender dropped: {received:?}"
    );
}

// ---------------------------------------------------------------------------
// Real-wake tests: require `--features real-wake` and the staged ONNX assets.
// ---------------------------------------------------------------------------

#[cfg(feature = "real-wake")]
mod real_wake_tests {
    use super::audio::AudioFrame;
    use super::config::Settings;
    use super::wake::{
        mel_smoke_for_test, models_load_for_test, should_trigger_for_test, OwwDetector,
        WakeDetector,
    };
    use std::path::PathBuf;
    use std::sync::Arc;
    use tokio::sync::mpsc;

    /// Resolve the absolute path to assets/openwakeword/alexa.onnx relative to
    /// the crate root (CARGO_MANIFEST_DIR is set by the test harness).
    fn alexa_onnx_path() -> PathBuf {
        let manifest = std::env::var("CARGO_MANIFEST_DIR")
            .expect("CARGO_MANIFEST_DIR not set");
        PathBuf::from(manifest)
            .join("assets")
            .join("openwakeword")
            .join("alexa.onnx")
    }

    /// Build a Settings with wake_model_path pointing at the staged alexa.onnx.
    fn test_settings() -> Arc<Settings> {
        Arc::new(Settings {
            wake_model_path: alexa_onnx_path(),
            ..Settings::default()
        })
    }

    /// Build a silent 80 ms PCM frame (1280 zero samples at 16 kHz mono).
    fn silent_frame() -> AudioFrame {
        AudioFrame::from_samples(&vec![0i16; 1280])
    }

    /// Test 1: OwwDetector initializes without error using the staged alexa.onnx.
    #[tokio::test]
    async fn test_real_detector_loads_models() {
        let settings = test_settings();
        assert!(
            alexa_onnx_path().exists(),
            "staged alexa.onnx not found at {:?}",
            alexa_onnx_path()
        );
        models_load_for_test(&settings).expect("all three wake models must load successfully");
    }

    /// Test 2: the bundled mel model accepts one normalized 80 ms frame.
    #[test]
    fn test_real_detector_mel_only_smoke() {
        let output = mel_smoke_for_test(&vec![0.0f32; 1280])
            .expect("bundled mel model must run on one 80 ms frame");
        assert_eq!(output.len(), 5 * 32);
        assert!(output.iter().all(|value| value.is_finite()));
    }

    #[test]
    fn test_real_detector_triggers_only_on_trailing_edge_after_debounce() {
        assert!(!should_trigger_for_test(0.8, 0.8, 0.5, 2_000));
        assert!(!should_trigger_for_test(0.1, 0.8, 0.5, 2_000));
        assert!(!should_trigger_for_test(0.05, 0.5, 0.5, 2_000));
        assert!(!should_trigger_for_test(0.05, 0.8, 0.5, 1_999));
        assert!(should_trigger_for_test(0.05, 0.8, 0.5, 2_000));
    }

    /// Test 3: an invalid classifier path is surfaced as an explicit fatal error.
    #[tokio::test]
    async fn test_real_detector_reports_invalid_classifier_path() {
        let temp_dir = tempfile::tempdir().expect("create temporary model directory");
        let missing_model = temp_dir.path().join("missing-wake.onnx");
        let settings = Arc::new(Settings {
            wake_model_path: missing_model,
            ..Settings::default()
        });
        let (_tx, rx) = mpsc::channel::<AudioFrame>(4);
        let detector = Box::new(OwwDetector::new(settings));
        let mut wake_rx = detector.start(rx);

        let error = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            wake_rx.recv(),
        )
        .await
        .expect("timed out waiting for initialization failure")
        .expect("wake channel closed without failure")
        .expect_err("invalid classifier path must fail initialization");

        assert!(error.to_string().contains("missing classifier model"));
    }

    /// Test 4: Feeding 30 seconds of silence must not produce any WakeEvent.
    #[tokio::test]
    async fn test_real_detector_no_false_positive_on_silence() {
        let settings = test_settings();
        let (tx, rx) = mpsc::channel::<AudioFrame>(64);
        let detector = Box::new(OwwDetector::new(settings));
        let mut wake_rx = detector.start(rx);

        tokio::time::timeout(std::time::Duration::from_secs(20), async {
            // 30_000 ms / 80 ms per frame = 375 frames.
            for _ in 0..375 {
                tx.send(silent_frame())
                    .await
                    .expect("wake detector input closed while feeding silence");
            }
        })
        .await
        .expect("timed out while feeding 30 seconds of silence");
        drop(tx);

        tokio::time::timeout(std::time::Duration::from_secs(20), async {
            loop {
                match wake_rx.recv().await {
                    Some(Ok(event)) => {
                        panic!("silence produced wake event at score {}", event.score)
                    }
                    Some(Err(error)) => panic!("wake detector failed: {error}"),
                    None => break,
                }
            }
        })
        .await
        .expect("wake detector did not finish processing 30 seconds of silence");
    }

    /// Test 5: Feed a known "alexa" WAV fixture and expect at least one WakeEvent.
    ///
    /// The fixture lives at tests/fixtures/alexa.wav and is sourced from the
    /// checked-in wyoming-openwakeword fixture set.
    #[tokio::test]
    async fn test_real_detector_fires_on_alexa_wav() {
        let manifest = std::env::var("CARGO_MANIFEST_DIR")
            .expect("CARGO_MANIFEST_DIR not set");
        let wav_path = PathBuf::from(&manifest)
            .join("tests")
            .join("fixtures")
            .join("alexa.wav");

        assert!(wav_path.exists(), "fixture not found at {wav_path:?}");

        // Read WAV and chunk into 1280-sample (80 ms) frames.
        let mut reader = hound::WavReader::open(&wav_path)
            .expect("failed to open alexa.wav");
        let spec = reader.spec();
        assert_eq!(spec.sample_rate, 16_000, "fixture must be 16 kHz");
        assert_eq!(spec.channels, 1, "fixture must be mono");

        let all_samples: Vec<i16> = reader
            .samples::<i16>()
            .map(|s| s.expect("WAV decode error"))
            .collect();

        let settings = test_settings();
        let (tx, rx) = mpsc::channel::<AudioFrame>(128);
        let detector = Box::new(OwwDetector::new(settings));
        let mut wake_rx = detector.start(rx);

        tokio::time::timeout(std::time::Duration::from_secs(20), async {
            // Send all frames from the WAV file.
            for chunk in all_samples.chunks(1280) {
                let mut padded = chunk.to_vec();
                if padded.len() < 1280 {
                    padded.resize(1280, 0);
                }
                tx.send(AudioFrame::from_samples(&padded))
                    .await
                    .expect("wake detector input closed while feeding alexa WAV");
            }
            // Add trailing silence to flush buffers through the pipeline.
            for _ in 0..32 {
                tx.send(AudioFrame::from_samples(&vec![0i16; 1280]))
                    .await
                    .expect("wake detector input closed while flushing alexa WAV");
            }
        })
        .await
        .expect("timed out while feeding alexa WAV");
        drop(tx);

        // Expect at least one WakeEvent within 5 seconds.
        let event = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            wake_rx.recv(),
        )
        .await
        .expect("timed out waiting for wake event on alexa WAV")
        .expect("wake_rx closed before event")
        .expect("wake detector failed while processing alexa WAV");

        assert!(
            event.detected_at_ms > 0,
            "WakeEvent has zero timestamp"
        );
        assert!(
            event.score >= test_settings().wake_threshold,
            "wake score {} is below threshold {}",
            event.score,
            test_settings().wake_threshold,
        );
    }
}
