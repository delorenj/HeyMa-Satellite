// tests/utterance.rs — UtteranceDetector silence detection + min/max guards.

#[path = "../src/audio.rs"]
mod audio;
#[path = "../src/config.rs"]
mod config;
#[path = "../src/utterance.rs"]
mod utterance;

use audio::AudioFrame;
use utterance::{compute_rms, make_utterance_detector, rms_to_dbfs, UtteranceDetector, UtteranceState};

const SAMPLE_RATE: u32 = 16_000;
const FRAME_SAMPLES: usize = 1280; // 80 ms at 16 kHz

/// Build a frame of all-zero (silence) samples.
fn silent_frame() -> AudioFrame {
    AudioFrame::from_samples(&vec![0i16; FRAME_SAMPLES])
}

/// Build a frame of a sine wave at given amplitude (0–32767).
fn sine_frame(amplitude: i16) -> AudioFrame {
    let samples: Vec<i16> = (0..FRAME_SAMPLES)
        .map(|i| {
            let angle = 2.0 * std::f64::consts::PI * 440.0 * i as f64 / SAMPLE_RATE as f64;
            (angle.sin() * amplitude as f64) as i16
        })
        .collect();
    AudioFrame::from_samples(&samples)
}

/// Compute expected silence threshold linear from dBFS.
fn db_to_linear(db: f32) -> f32 {
    10_f32.powf(db / 20.0)
}

// ---------------------------------------------------------------------------
// RMS helper tests
// ---------------------------------------------------------------------------

#[test]
fn test_rms_of_silence_is_zero() {
    let samples = vec![0i16; 1280];
    assert_eq!(compute_rms(&samples), 0.0);
}

#[test]
fn test_rms_of_full_scale_sine() {
    // Full-scale sine has RMS = amplitude / sqrt(2) ≈ 0.707.
    let samples: Vec<i16> = (0..1280)
        .map(|i| {
            let angle = 2.0 * std::f64::consts::PI * 440.0 * i as f64 / 16_000.0;
            (angle.sin() * 32767.0) as i16
        })
        .collect();
    let rms = compute_rms(&samples);
    // Expect roughly 0.7 ± 0.05 after i16 quantization.
    assert!(
        (rms - 0.707).abs() < 0.05,
        "full-scale sine RMS should be ~0.707, got {rms}"
    );
}

#[test]
fn test_rms_dbfs_of_silence_is_neg_inf() {
    assert_eq!(rms_to_dbfs(0.0), f32::NEG_INFINITY);
}

#[test]
fn test_rms_dbfs_of_full_scale_is_near_zero() {
    // Full-scale sine: ~0.707 linear → ~-3 dBFS.
    let rms_db = rms_to_dbfs(0.707);
    assert!(
        (rms_db - (-3.01)).abs() < 0.1,
        "expected ~-3 dBFS, got {rms_db}"
    );
}

// ---------------------------------------------------------------------------
// UtteranceDetector state machine tests
// ---------------------------------------------------------------------------

/// Create a detector with default config-like values (300 ms silence hold).
fn make_det(silence_threshold_db: f32, min_ms: u32, max_ms: u32) -> UtteranceDetector {
    make_utterance_detector(SAMPLE_RATE, silence_threshold_db, min_ms, max_ms, 300)
}

#[test]
fn test_silence_does_not_trigger_before_min_utterance() {
    // min = 500 ms → 7 frames of 80 ms = 560 ms crosses threshold.
    // First 6 frames = 480 ms → still under min.
    let mut det = make_det(-40.0, 500, 30_000);

    for i in 0..6 {
        let state = det.push_frame(&silent_frame());
        assert_eq!(
            state,
            UtteranceState::Listening,
            "frame {i}: should still be Listening before min_utterance_ms"
        );
    }
}

#[test]
fn test_silence_fires_end_of_input_after_min_utterance_and_hold() {
    // min = 200 ms (3 frames), hold = 300 ms (4 frames silence after min).
    // So: 3 silent frames to satisfy min, then 4 more silent frames → EOU.
    // Total = 7 frames (560 ms). But silence_threshold_db = -40, and silent
    // frames have RMS 0.0 which is below the threshold.
    let mut det = make_utterance_detector(SAMPLE_RATE, -40.0, 200, 30_000, 300);

    // 7 silent frames × 80 ms = 560 ms > min(200) + hold(300) = 500 ms.
    let mut last_state = UtteranceState::Listening;
    for _ in 0..7 {
        last_state = det.push_frame(&silent_frame());
        if last_state != UtteranceState::Listening {
            break;
        }
    }
    assert_eq!(
        last_state,
        UtteranceState::EndOfInput,
        "should fire EndOfInput after min + silence hold"
    );
}

#[test]
fn test_max_utterance_fires_unconditionally() {
    // max = 160 ms = 2 frames.
    let mut det = make_utterance_detector(SAMPLE_RATE, -40.0, 80, 160, 300);

    // First frame: 80 ms elapsed, < max.
    let s1 = det.push_frame(&sine_frame(10_000));
    // Second frame: 160 ms elapsed = max.
    let s2 = det.push_frame(&sine_frame(10_000));

    // The second frame should trigger MaxDurationReached (or EndOfInput if
    // silence hold was also satisfied — we use loud sine so silence_hold won't fire).
    assert!(
        s2 == UtteranceState::MaxDurationReached,
        "max duration not reached; got {s2:?} (s1={s1:?})"
    );
}

#[test]
fn test_signal_resets_silence_streak() {
    // min = 80 ms (1 frame), hold = 240 ms (3 frames post-min silence).
    // Frame timeline (each frame = 80ms):
    //   frame 1: silent — elapsed=80=min, streak=80ms
    //   frame 2: silent — streak=160ms
    //   frame 3: LOUD  — streak reset to 0
    //   frame 4: silent — streak=80ms
    //   frame 5: silent — streak=160ms
    //   frame 6: silent — streak=240ms >= hold → EndOfInput
    let mut det = make_utterance_detector(SAMPLE_RATE, -40.0, 80, 30_000, 240);

    // Frame 1: satisfies min; silence streak starts.
    assert_eq!(det.push_frame(&silent_frame()), UtteranceState::Listening); // streak=80

    // Frame 2: streak grows but < hold.
    assert_eq!(det.push_frame(&silent_frame()), UtteranceState::Listening); // streak=160

    // Frame 3: loud signal resets streak.
    assert_eq!(det.push_frame(&sine_frame(10_000)), UtteranceState::Listening); // streak=0

    // Frames 4-5: silence rebuilding streak (still < hold=240).
    assert_eq!(det.push_frame(&silent_frame()), UtteranceState::Listening); // streak=80
    assert_eq!(det.push_frame(&silent_frame()), UtteranceState::Listening); // streak=160

    // Frame 6: streak=240 >= hold → EndOfInput.
    let final_state = det.push_frame(&silent_frame()); // streak=240
    assert_eq!(
        final_state,
        UtteranceState::EndOfInput,
        "after loud-reset, 3 more silent frames should trigger EOU (streak=240ms = hold)"
    );
}

#[test]
fn test_reset_clears_state() {
    let mut det = make_utterance_detector(SAMPLE_RATE, -40.0, 80, 30_000, 160);

    // Push enough to trigger min then some silence.
    det.push_frame(&silent_frame());
    det.push_frame(&silent_frame());

    // Reset.
    det.reset();

    // After reset, first frame should not trigger EOU.
    let state = det.push_frame(&silent_frame());
    assert_eq!(
        state,
        UtteranceState::Listening,
        "after reset, first frame should be Listening"
    );
}

#[test]
fn test_loud_frames_never_trigger_silence_detection() {
    // Loud sine (amplitude ~28000, well above -40 dBFS threshold).
    let mut det = make_det(-40.0, 80, 30_000);

    // Push 100 loud frames (8 seconds of audio) — should stay Listening.
    for i in 0..100 {
        let state = det.push_frame(&sine_frame(28_000));
        assert_ne!(
            state,
            UtteranceState::EndOfInput,
            "frame {i}: loud audio should never trigger EndOfInput"
        );
    }
}

// ---------------------------------------------------------------------------
// Extra: public make_utterance_detector factory with default hold=300ms
// ---------------------------------------------------------------------------

#[test]
fn test_factory_creates_valid_detector() {
    let mut det = make_utterance_detector(16_000, -40.0, 500, 30_000, 300);
    // Smoke: doesn't panic with default hold.
    let _ = det.push_frame(&silent_frame());
}
