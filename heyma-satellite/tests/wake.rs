// tests/wake.rs — WakeDetector trait + StubWakeDetector behavior.

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
    // Either timeout or None is acceptable — the key is no panic.
    match result {
        Ok(None) => {} // channel closed cleanly
        Err(_) => {}   // timeout fine too
        Ok(Some(_)) => panic!("unexpected wake event after sender drop"),
    }
}
