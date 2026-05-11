// Wake word detection seam.
//
// Architecture: the WakeDetector trait abstracts over two implementations.
//
//   StubWakeDetector  -- default build, no model file needed, fires on sentinel.
//   OwwDetector       -- real-wake feature, 3-stage ONNX pipeline via tract-onnx.
//
// The OwwDetector runs openwakeword's published pipeline directly:
//   melspectrogram.onnx  -> embedding_model.onnx  -> <classifier>.onnx
// No oww-rs dependency. Models are described in assets/openwakeword/.
// Universal models (mel, embedding) are bundled at compile time via
// include_bytes!. The per-wake-word classifier loads at runtime from
// Settings.wake_model_path.

use crate::audio::AudioFrame;
use crate::config::Settings;
use std::sync::Arc;
use tokio::sync::mpsc;

// ---------------------------------------------------------------------------
// Domain types
// ---------------------------------------------------------------------------

/// Fired when the wake detector crosses the confidence threshold.
#[derive(Debug, Clone)]
pub struct WakeEvent {
    /// Monotonic timestamp (ms since epoch, best-effort).
    pub detected_at_ms: u64,
}

// ---------------------------------------------------------------------------
// WakeDetector trait
// ---------------------------------------------------------------------------

/// Trait seam for wake-word detection.
pub trait WakeDetector: Send + 'static {
    /// Consume audio frames from `rx`, emit a `WakeEvent` each time the model
    /// scores above the configured threshold. Returns a new channel for wake events.
    ///
    /// F8: if initialization fails, the implementation must propagate the failure
    /// via the returned JoinHandle rather than silently returning. The supervisor
    /// awaits the handle and treats Err as fatal.
    fn start(
        self: Box<Self>,
        rx: mpsc::Receiver<AudioFrame>,
    ) -> mpsc::Receiver<WakeEvent>;
}

// ---------------------------------------------------------------------------
// OwwDetector -- production implementation (requires real-wake feature)
// ---------------------------------------------------------------------------

#[cfg(feature = "real-wake")]
mod real_wake {
    use super::{AudioFrame, Settings, WakeDetector, WakeEvent};
    use std::collections::VecDeque;
    use std::io::Cursor;
    use std::sync::Arc;
    use std::time::{Duration, Instant};
    use tokio::sync::mpsc;
    use tract_onnx::prelude::*;

    // Pipeline constants (mirrored from oww-rs 0.2.0 source).
    // Mel buffer: 16 slots of [5, 32] frames. Stack -> [80, 32], slice [4:80] -> [76, 32].
    const MEL_BUFFER_SIZE: usize = 16;
    // Embedding buffer: 16 slots of [96] vectors. Stack -> [16, 96].
    const EMBEDDING_BUFFER_SIZE: usize = 16;
    // Detection probability buffer: 12 slots for moving average.
    const DETECTION_BUFFER_SIZE: usize = 12;
    // Minimum number of supra-threshold scores required before avg is trusted.
    const MIN_POSITIVE_DETECTIONS: f32 = 3.0;
    // Minimum milliseconds between consecutive detections (debounce).
    const DEBOUNCE_MS: u64 = 2_000;
    // Score below this is ignored for moving-average purposes.
    const SCORE_FLOOR: f32 = 0.1;

    // Universal model bytes bundled at compile time.
    static MEL_MODEL_BYTES: &[u8] =
        include_bytes!("../assets/openwakeword/melspectrogram.onnx");
    static EMBEDDING_MODEL_BYTES: &[u8] =
        include_bytes!("../assets/openwakeword/embedding_model.onnx");

    /// Error type for OwwDetector initialization.
    #[derive(Debug)]
    pub enum WakeInitError {
        TractError(tract_onnx::prelude::TractError),
        IoError(std::io::Error),
        MissingModel(String),
    }

    impl std::fmt::Display for WakeInitError {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            match self {
                WakeInitError::TractError(e) => write!(f, "tract error: {e}"),
                WakeInitError::IoError(e) => write!(f, "io error: {e}"),
                WakeInitError::MissingModel(p) => write!(f, "missing classifier model: {p}"),
            }
        }
    }

    impl From<tract_onnx::prelude::TractError> for WakeInitError {
        fn from(e: tract_onnx::prelude::TractError) -> Self {
            WakeInitError::TractError(e)
        }
    }

    impl From<std::io::Error> for WakeInitError {
        fn from(e: std::io::Error) -> Self {
            WakeInitError::IoError(e)
        }
    }

    // Runnable model type alias for clarity.
    type RunnableOnnx = RunnableModel<TypedFact, Box<dyn TypedOp>, Graph<TypedFact, Box<dyn TypedOp>>>;

    pub struct OwwDetector {
        settings: Arc<Settings>,
    }

    impl OwwDetector {
        pub fn new(settings: Arc<Settings>) -> Self {
            OwwDetector { settings }
        }

        /// Load and optimize a model from raw bytes with the given input shape.
        fn load_model_from_bytes(
            bytes: &[u8],
            input_shape: &[usize],
        ) -> Result<RunnableOnnx, WakeInitError> {
            let mut rdr = Cursor::new(bytes);
            let fact = f32::fact(input_shape.to_vec());
            let model = tract_onnx::onnx()
                .model_for_read(&mut rdr)?
                .with_input_fact(0, fact.into())?
                .into_optimized()?
                .into_runnable()?;
            Ok(model)
        }

        /// Load and optimize the classifier model from a file path.
        fn load_classifier(path: &std::path::Path) -> Result<RunnableOnnx, WakeInitError> {
            if !path.exists() {
                return Err(WakeInitError::MissingModel(
                    path.display().to_string(),
                ));
            }
            let bytes = std::fs::read(path)?;
            let mut rdr = Cursor::new(bytes);
            let fact = f32::fact(vec![1usize, 16, 96]);
            let model = tract_onnx::onnx()
                .model_for_read(&mut rdr)?
                .with_input_fact(0, fact.into())?
                .into_optimized()?
                .into_runnable()?;
            Ok(model)
        }
    }

    impl WakeDetector for OwwDetector {
        fn start(
            self: Box<Self>,
            mut rx: mpsc::Receiver<AudioFrame>,
        ) -> mpsc::Receiver<WakeEvent> {
            let (tx, wake_rx) = mpsc::channel(4);
            let settings = self.settings.clone();

            tokio::spawn(async move {
                // All model loading happens in the spawned task on a blocking thread
                // to avoid blocking the async executor during ONNX optimization.
                let load_result: Result<
                    (RunnableOnnx, RunnableOnnx, RunnableOnnx),
                    WakeInitError,
                > = tokio::task::spawn_blocking({
                    let settings = settings.clone();
                    move || {
                        let mel = OwwDetector::load_model_from_bytes(
                            MEL_MODEL_BYTES,
                            &[1, 1280],
                        )?;
                        let emb = OwwDetector::load_model_from_bytes(
                            EMBEDDING_MODEL_BYTES,
                            &[1, 76, 32, 1],
                        )?;
                        let cls = OwwDetector::load_classifier(&settings.wake_model_path)?;
                        Ok((mel, emb, cls))
                    }
                })
                .await
                .unwrap_or_else(|join_err| {
                    Err(WakeInitError::MissingModel(format!(
                        "spawn_blocking panicked: {join_err}"
                    )))
                });

                let (mel_model, emb_model, cls_model) = match load_result {
                    Ok(models) => models,
                    Err(e) => {
                        tracing::error!(
                            event = "wake_init_failed",
                            path = %settings.wake_model_path.display(),
                            error = %e,
                        );
                        return;
                    }
                };

                tracing::info!(
                    event = "real_wake_initialized",
                    model_path = %settings.wake_model_path.display(),
                );

                // FIFO ring buffers.
                // mel_buf: each entry is a flattened [5*32 = 160] f32 array.
                let mut mel_buf: VecDeque<Vec<f32>> =
                    std::iter::repeat_with(|| vec![0f32; 5 * 32])
                        .take(MEL_BUFFER_SIZE)
                        .collect();
                // emb_buf: each entry is a flattened [96] f32 array.
                let mut emb_buf: VecDeque<Vec<f32>> =
                    std::iter::repeat_with(|| vec![0f32; 96])
                        .take(EMBEDDING_BUFFER_SIZE)
                        .collect();
                // det_buf: ring of raw scores for moving-average detection.
                let mut det_buf: VecDeque<f32> =
                    std::iter::repeat(0f32).take(DETECTION_BUFFER_SIZE).collect();

                let mut last_detection = Instant::now()
                    - Duration::from_millis(DEBOUNCE_MS + 1);

                while let Some(frame) = rx.recv().await {
                    // Stage 0: i16 -> f32 normalization.
                    let samples_f32: Vec<f32> = frame
                        .as_samples()
                        .iter()
                        .map(|&s| s as f32 / 32768.0)
                        .collect();

                    // Stage 1: mel inference.
                    // Input shape: [1, 1280]. Output shape: [5, 32].
                    let mel_out = match run_mel(&mel_model, &samples_f32) {
                        Ok(v) => v,
                        Err(e) => {
                            tracing::warn!(event = "mel_inference_error", error = %e);
                            continue;
                        }
                    };
                    // Push to mel buffer (FIFO: drop oldest, add newest).
                    mel_buf.pop_front();
                    mel_buf.push_back(mel_out);

                    // Stage 2: embedding inference.
                    // Stack MEL_BUFFER_SIZE x [5, 32] -> [80, 32], slice [4:80] -> [76, 32],
                    // reshape to [1, 76, 32, 1].
                    let emb_out = match run_embedding(&emb_model, &mel_buf) {
                        Ok(v) => v,
                        Err(e) => {
                            tracing::warn!(event = "emb_inference_error", error = %e);
                            continue;
                        }
                    };
                    // Push to embedding buffer.
                    emb_buf.pop_front();
                    emb_buf.push_back(emb_out);

                    // Stage 3: classifier inference.
                    // Stack EMBEDDING_BUFFER_SIZE x [96] -> [16, 96], reshape [1, 16, 96].
                    let score = match run_classifier(&cls_model, &emb_buf) {
                        Ok(s) => s,
                        Err(e) => {
                            tracing::warn!(event = "cls_inference_error", error = %e);
                            continue;
                        }
                    };

                    // Push raw score to detection buffer.
                    det_buf.pop_front();
                    det_buf.push_back(score);

                    // Detection rule: trailing-edge gate + moving average + debounce.
                    let avg = calculate_average(&det_buf);
                    let since_last_ms = last_detection.elapsed().as_millis() as u64;

                    if score < SCORE_FLOOR
                        && avg > settings.wake_threshold
                        && since_last_ms > DEBOUNCE_MS
                    {
                        last_detection = Instant::now();
                        tracing::info!(
                            event = "wake_score_above_threshold",
                            score = score,
                            threshold = settings.wake_threshold,
                        );
                        let now_ms = std::time::SystemTime::now()
                            .duration_since(std::time::UNIX_EPOCH)
                            .unwrap_or_default()
                            .as_millis() as u64;
                        if tx.send(WakeEvent { detected_at_ms: now_ms }).await.is_err() {
                            break;
                        }
                    }
                }
            });

            wake_rx
        }
    }

    // ---------------------------------------------------------------------------
    // Stage helpers: all run on the calling async task (single-threaded).
    // ---------------------------------------------------------------------------

    /// Run mel-spectrogram inference.
    /// Input: 1280 f32 samples. Output: 160 f32 values representing [5, 32].
    /// Applies oww-rs normalization: v = v / 10.0 + 2.0.
    fn run_mel(
        model: &RunnableOnnx,
        samples: &[f32],
    ) -> Result<Vec<f32>, TractError> {
        let input = Tensor::from_shape(&[1, 1280], samples)?;
        let outputs = model.run(tvec!(input.into()))?;
        let out_tensor = outputs[0].clone().into_tensor();
        // Reshape to [5, 32] to match oww-rs convention, then flatten.
        let reshaped = out_tensor.into_shape(&[5, 32])?;
        let arr = reshaped.into_array::<f32>()?;
        // Apply oww-rs normalization: v / 10.0 + 2.0.
        let normalized: Vec<f32> = arr.iter().map(|&v| v / 10.0 + 2.0).collect();
        Ok(normalized)
    }

    /// Run embedding inference.
    /// Stacks mel_buf (16 x [5,32] = [80, 32]), slices rows [4:80] -> [76, 32],
    /// reshapes to [1, 76, 32, 1], runs embedding model.
    /// Output: 96 f32 values representing [96].
    fn run_embedding(
        model: &RunnableOnnx,
        mel_buf: &VecDeque<Vec<f32>>,
    ) -> Result<Vec<f32>, TractError> {
        // Stack: [MEL_BUFFER_SIZE * 5, 32] = [80, 32].
        let mut stacked = vec![0f32; MEL_BUFFER_SIZE * 5 * 32];
        for (i, mel_frame) in mel_buf.iter().enumerate() {
            // Each mel_frame is [5, 32] = 160 values.
            let offset = i * 5 * 32;
            stacked[offset..offset + 5 * 32].copy_from_slice(mel_frame);
        }
        // Slice rows [4:80]: skip first 4 rows (4*32 = 128 values) -> [76, 32].
        let sliced: Vec<f32> = stacked[4 * 32..80 * 32].to_vec();
        // Reshape to [1, 76, 32, 1].
        let input = Tensor::from_shape(&[1, 76, 32, 1], &sliced)?;
        let outputs = model.run(tvec!(input.into()))?;
        // Output shape: [1, 1, 1, 96]. Flatten to [96].
        let out_tensor = outputs[0].clone().into_tensor();
        let flat = out_tensor.into_shape(&[96])?;
        let arr = flat.into_array::<f32>()?;
        Ok(arr.into_raw_vec_and_offset().0)
    }

    /// Run classifier inference.
    /// Stacks emb_buf (16 x [96]) -> [16, 96], reshapes to [1, 16, 96].
    /// Output: single f32 score from [1, 1] output.
    fn run_classifier(
        model: &RunnableOnnx,
        emb_buf: &VecDeque<Vec<f32>>,
    ) -> Result<f32, TractError> {
        // Stack: [16, 96].
        let mut stacked = vec![0f32; EMBEDDING_BUFFER_SIZE * 96];
        for (i, emb_vec) in emb_buf.iter().enumerate() {
            let offset = i * 96;
            stacked[offset..offset + 96].copy_from_slice(emb_vec);
        }
        // Reshape to [1, 16, 96].
        let input = Tensor::from_shape(&[1, 16, 96], &stacked)?;
        let outputs = model.run(tvec!(input.into()))?;
        // Output shape: [1, 1]. Extract scalar score.
        let out_tensor = outputs[0].clone().into_tensor();
        let casted = out_tensor.cast_to::<f32>()?;
        let score = casted.as_slice::<f32>()?[0];
        Ok(score)
    }

    /// Moving-average detection rule, mirrored from oww-rs oww_model.rs.
    ///
    /// Algorithm:
    ///   1. Iterate detection buffer.
    ///   2. Scores below SCORE_FLOOR (0.1) are ignored.
    ///   3. Sum supra-floor scores; count them.
    ///   4. If count > MIN_POSITIVE_DETECTIONS (3) and avg > threshold, return avg.
    ///   5. Otherwise return 0.0.
    ///
    /// The caller fires a WakeEvent when:
    ///   current_score < SCORE_FLOOR  (trailing-edge gate)
    ///   AND calculate_average() > wake_threshold
    ///   AND debounce elapsed
    pub fn calculate_average(det_buf: &VecDeque<f32>) -> f32 {
        let mut cumulative = 0.0f32;
        let mut positive_count = 0.0f32;
        for &d in det_buf.iter() {
            if d > SCORE_FLOOR {
                positive_count += 1.0;
                cumulative += d;
            }
        }
        if positive_count == 0.0 {
            return 0.0;
        }
        let avg = cumulative / positive_count;
        if positive_count > MIN_POSITIVE_DETECTIONS {
            avg
        } else {
            0.0
        }
    }

}

#[cfg(feature = "real-wake")]
pub use real_wake::OwwDetector;

// ---------------------------------------------------------------------------
// StubWakeDetector -- used in tests and default builds without a model file
// ---------------------------------------------------------------------------

/// A deterministic stub detector for tests. Fires a `WakeEvent` when it receives
/// a frame whose first sample (little-endian i16) equals the sentinel value
/// `WAKE_SENTINEL` (0x7E57 = 32343, "WAKE" mnemonic).
pub const WAKE_SENTINEL: i16 = 0x7E57;

pub struct StubWakeDetector;

impl WakeDetector for StubWakeDetector {
    fn start(
        self: Box<Self>,
        mut rx: mpsc::Receiver<AudioFrame>,
    ) -> mpsc::Receiver<WakeEvent> {
        let (tx, wake_rx) = mpsc::channel(4);

        tokio::spawn(async move {
            while let Some(frame) = rx.recv().await {
                let samples = frame.as_samples();
                if samples.first() == Some(&WAKE_SENTINEL) {
                    let now_ms = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_millis() as u64;
                    if tx.send(WakeEvent { detected_at_ms: now_ms }).await.is_err() {
                        break;
                    }
                }
            }
        });

        wake_rx
    }
}

// ---------------------------------------------------------------------------
// Convenience: select detector based on feature flags and config
// ---------------------------------------------------------------------------

/// Return the appropriate boxed detector. In a `real-wake` build, returns
/// `OwwDetector`; otherwise returns `StubWakeDetector`.
pub fn make_detector(settings: Arc<Settings>) -> Box<dyn WakeDetector> {
    #[cfg(feature = "real-wake")]
    {
        Box::new(OwwDetector::new(settings))
    }
    #[cfg(not(feature = "real-wake"))]
    {
        let _ = settings; // suppress unused warning
        Box::new(StubWakeDetector)
    }
}
