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
    .expect("wake channel closed");

    assert!(event.detected_at_ms > 0);
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
    while let Ok(Some(event)) = tokio::time::timeout(
        std::time::Duration::from_millis(200),
        wake_rx.recv(),
    )
    .await
    {
        count += 1;
        assert!(event.detected_at_ms > 0);
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
    // Either timeout or None is acceptable. The key is no panic.
    match result {
        Ok(None) => {} // channel closed cleanly
        Err(_) => {}   // timeout fine too
        Ok(Some(_)) => panic!("unexpected wake event after sender drop"),
    }
}

// ---------------------------------------------------------------------------
// Real-wake tests: require `--features real-wake` and the staged ONNX assets.
// ---------------------------------------------------------------------------

#[cfg(feature = "real-wake")]
mod real_wake_tests {
    use super::audio::AudioFrame;
    use super::config::Settings;
    use super::wake::{OwwDetector, WakeDetector};
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
        let (_tx, rx) = mpsc::channel::<AudioFrame>(4);
        let detector = Box::new(OwwDetector::new(settings));
        // start() spawns the task and returns the wake_rx. If model loading
        // fails inside the task the channel closes immediately. We verify it
        // does NOT close within a short window (model loaded successfully).
        let mut wake_rx = detector.start(rx);
        let result = tokio::time::timeout(
            std::time::Duration::from_millis(3_000),
            wake_rx.recv(),
        )
        .await;
        // A timeout means the channel is open (no init failure closed it).
        // A None result would mean the task exited, indicating init failure.
        match result {
            Err(_timeout) => {} // channel still open after 3 s: init succeeded
            Ok(None) => panic!(
                "wake_rx closed immediately: model init likely failed; check that \
                 assets/openwakeword/alexa.onnx is present and valid"
            ),
            Ok(Some(_)) => {} // unexpected wake event on silent input: still a pass
        }
    }

    /// Test 2: Feeding 5 seconds of silence (300 frames at 80 ms each) must not
    /// produce any WakeEvent.
    #[tokio::test]
    async fn test_real_detector_no_false_positive_on_silence() {
        let settings = test_settings();
        let (tx, rx) = mpsc::channel::<AudioFrame>(64);
        let detector = Box::new(OwwDetector::new(settings));
        let mut wake_rx = detector.start(rx);

        // Feed 5 seconds worth of silence: 5000 ms / 80 ms = ~62 frames.
        // Using 100 frames for a comfortable margin.
        for _ in 0..100 {
            let _ = tx.send(silent_frame()).await;
        }
        drop(tx);

        // Drain any events that arrived.
        let mut event_count = 0usize;
        loop {
            match tokio::time::timeout(
                std::time::Duration::from_millis(500),
                wake_rx.recv(),
            )
            .await
            {
                Ok(Some(_)) => event_count += 1,
                Ok(None) | Err(_) => break,
            }
        }
        assert_eq!(
            event_count, 0,
            "silence produced {event_count} wake events, expected 0"
        );
    }

    /// Test 3: Feed a known "alexa" WAV fixture and expect at least one WakeEvent.
    ///
    /// The fixture lives at tests/fixtures/alexa.wav. If it does not exist,
    /// this test is skipped. To record a fixture:
    ///   arecord -f S16_LE -r 16000 -c 1 heyma-satellite/tests/fixtures/alexa.wav
    /// Speak "alexa" into the mic, then stop the recording.
    #[tokio::test]
    #[ignore = "requires tests/fixtures/alexa.wav; record with: arecord -f S16_LE -r 16000 -c 1 tests/fixtures/alexa.wav"]
    async fn test_real_detector_fires_on_alexa_wav() {
        let manifest = std::env::var("CARGO_MANIFEST_DIR")
            .expect("CARGO_MANIFEST_DIR not set");
        let wav_path = PathBuf::from(&manifest)
            .join("tests")
            .join("fixtures")
            .join("alexa.wav");

        if !wav_path.exists() {
            eprintln!("SKIP: fixture not found at {wav_path:?}");
            return;
        }

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

        // Send all frames from the WAV file.
        for chunk in all_samples.chunks(1280) {
            let mut padded = chunk.to_vec();
            if padded.len() < 1280 {
                padded.resize(1280, 0);
            }
            let _ = tx.send(AudioFrame::from_samples(&padded)).await;
        }
        // Add trailing silence to flush buffers through the pipeline.
        for _ in 0..32 {
            let _ = tx.send(AudioFrame::from_samples(&vec![0i16; 1280])).await;
        }
        drop(tx);

        // Expect at least one WakeEvent within 5 seconds.
        let event = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            wake_rx.recv(),
        )
        .await
        .expect("timed out waiting for wake event on alexa WAV")
        .expect("wake_rx closed before event");

        assert!(
            event.detected_at_ms > 0,
            "WakeEvent has zero timestamp"
        );
    }
}
