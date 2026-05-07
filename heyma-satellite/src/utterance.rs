use crate::audio::AudioFrame;

// ---------------------------------------------------------------------------
// End-of-utterance detection
// ---------------------------------------------------------------------------


/// Per-utterance state, driven by `UtteranceDetector::push_frame`.
#[derive(Debug, Clone, PartialEq)]
pub enum UtteranceState {
    /// Still collecting audio; below silence threshold or under `min_utterance_ms`.
    Listening,
    /// Silence sustained long enough after `min_utterance_ms`: fire EOU.
    EndOfInput,
    /// `max_utterance_ms` ceiling hit unconditionally.
    MaxDurationReached,
}

/// RMS-based end-of-utterance detector.
///
/// Logic:
///   - Accumulate frames, tracking total elapsed ms.
///   - Once elapsed >= `min_utterance_ms`, start monitoring RMS.
///   - If RMS < `silence_threshold_linear` for `silence_hold_ms` consecutive ms,
///     emit `EndOfInput`.
///   - If elapsed >= `max_utterance_ms`, emit `MaxDurationReached` unconditionally.
pub struct UtteranceDetector {
    sample_rate: u32,
    silence_threshold_linear: f32,
    min_utterance_ms: u32,
    max_utterance_ms: u32,

    /// Consecutive milliseconds of silence observed (after min guard satisfied).
    silence_streak_ms: u32,
    /// Total milliseconds of audio observed in this utterance.
    elapsed_ms: u32,
    /// How many ms of consecutive silence required before EOU fires.
    silence_hold_ms: u32,
}

impl UtteranceDetector {
    /// Construct a detector from config-like parameters.
    ///
    /// `silence_threshold_db` — dBFS floor (e.g. -40.0).
    /// `silence_hold_ms` — how long silence must be sustained (e.g. 300 ms).
    pub fn new(
        sample_rate: u32,
        silence_threshold_db: f32,
        min_utterance_ms: u32,
        max_utterance_ms: u32,
        silence_hold_ms: u32,
    ) -> Self {
        // Convert dBFS to linear amplitude ratio: amplitude = 10^(dB/20).
        let silence_threshold_linear = 10_f32.powf(silence_threshold_db / 20.0);
        UtteranceDetector {
            sample_rate,
            silence_threshold_linear,
            min_utterance_ms,
            max_utterance_ms,
            silence_streak_ms: 0,
            elapsed_ms: 0,
            silence_hold_ms,
        }
    }

    /// Reset internal state for a new utterance.
    pub fn reset(&mut self) {
        self.silence_streak_ms = 0;
        self.elapsed_ms = 0;
    }

    /// Push one `AudioFrame` and return the current `UtteranceState`.
    ///
    /// Frames are assumed to be 80 ms of S16_LE mono at the configured sample rate.
    pub fn push_frame(&mut self, frame: &AudioFrame) -> UtteranceState {
        let samples = frame.as_samples();
        let frame_ms = self.frame_duration_ms(&samples);
        self.elapsed_ms += frame_ms;

        let rms = compute_rms(&samples);

        if self.elapsed_ms >= self.max_utterance_ms {
            return UtteranceState::MaxDurationReached;
        }

        if self.elapsed_ms >= self.min_utterance_ms {
            if rms < self.silence_threshold_linear {
                self.silence_streak_ms += frame_ms;
                if self.silence_streak_ms >= self.silence_hold_ms {
                    return UtteranceState::EndOfInput;
                }
            } else {
                // Sound detected — reset silence streak.
                self.silence_streak_ms = 0;
            }
        }

        UtteranceState::Listening
    }

    fn frame_duration_ms(&self, samples: &[i16]) -> u32 {
        if self.sample_rate == 0 || samples.is_empty() {
            return 0;
        }
        // samples.len() / sample_rate * 1000, integer arithmetic.
        (samples.len() as u64 * 1000 / self.sample_rate as u64) as u32
    }
}

/// Compute RMS amplitude of a slice of i16 samples, normalized to [0.0, 1.0].
pub fn compute_rms(samples: &[i16]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum_sq: f64 = samples.iter().map(|&s| (s as f64 / 32768.0).powi(2)).sum();
    (sum_sq / samples.len() as f64).sqrt() as f32
}

/// Compute RMS in dBFS (-inf..0]. Returns -f32::INFINITY for silence.
#[allow(dead_code)]
pub fn rms_to_dbfs(rms: f32) -> f32 {
    if rms <= 0.0 {
        return f32::NEG_INFINITY;
    }
    20.0 * rms.log10()
}

// ---------------------------------------------------------------------------
// Factory helper
// ---------------------------------------------------------------------------

/// Create a `UtteranceDetector`.
///
/// `silence_hold_ms` is how long sustained silence must be observed (after
/// `min_utterance_ms` is satisfied) before EOU fires. The production default
/// is 300 ms; pass 300 for normal use.
pub fn make_utterance_detector(
    sample_rate: u32,
    silence_threshold_db: f32,
    min_utterance_ms: u32,
    max_utterance_ms: u32,
    silence_hold_ms: u32,
) -> UtteranceDetector {
    UtteranceDetector::new(
        sample_rate,
        silence_threshold_db,
        min_utterance_ms,
        max_utterance_ms,
        silence_hold_ms,
    )
}
