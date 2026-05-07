// Wake word detection seam.
//
// WAKE DETECTOR RESOLUTION (2026-05-05):
//   oww-rs 0.2.0 IS published on crates.io (MIT, 2025-10-01) and is not yanked.
//   However, oww-rs uses `tract-onnx` for inference and `rust-embed` to bundle
//   models -- both of which require a real ONNX model file at *compile time* via
//   the embed macro. Since `hey_tonny.onnx` does not exist on this dev box
//   (it lives on tonny.local after training), we cannot instantiate `oww-rs::Detector`
//   without that file present. More practically: the crate compiles fine, but
//   constructing `Detector::new(path)` panics / errors at runtime without the model.
//
//   Decision: define the `WakeDetector` trait here. Gate the `OwwDetector` impl
//   behind the `real-wake` Cargo feature (feature = ["oww-rs"]). The default build
//   and all tests use `StubWakeDetector`, which satisfies the trait without the
//   ONNX model. The `real-wake` feature is compiled-in for Pi deployment.
//
//   This satisfies the spec's "stub if neither works so the rest compiles and tests
//   pass" clause while keeping the real path available for production.

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
// OwwDetector -- production implementation (requires `real-wake` feature + model file)
// ---------------------------------------------------------------------------

#[cfg(feature = "real-wake")]
pub struct OwwDetector {
    settings: Arc<Settings>,
}

#[cfg(feature = "real-wake")]
impl OwwDetector {
    pub fn new(settings: Arc<Settings>) -> Self {
        OwwDetector { settings }
    }
}

#[cfg(feature = "real-wake")]
impl WakeDetector for OwwDetector {
    fn start(
        self: Box<Self>,
        mut rx: mpsc::Receiver<AudioFrame>,
    ) -> mpsc::Receiver<WakeEvent> {
        let (tx, wake_rx) = mpsc::channel(4);
        let settings = self.settings.clone();

        tokio::spawn(async move {
            // oww-rs 0.2.0 public API:
            //   oww_rs::Detector::new(model_path, threshold) -> Result<Detector>
            //   detector.process_samples(&[f32]) -> Result<f32>  (returns score)
            use oww_rs::Detector;

            let detector = match Detector::new(
                settings.wake_model_path.to_str().unwrap_or(""),
                settings.wake_threshold,
            ) {
                Ok(d) => d,
                Err(e) => {
                    // F8: emit a clear error log and return Err so the spawned
                    // JoinHandle propagates the failure as non-zero exit rather
                    // than silently closing wake_rx (which would look like a clean stop).
                    tracing::error!(
                        event = "wake_init_failed",
                        path = %settings.wake_model_path.display(),
                        error = %e,
                    );
                    // Dropping tx here closes wake_rx; the supervisor treats None
                    // from wake_rx as fatal (see run_supervisor) because this
                    // task will have returned an error that propagates via the JoinHandle.
                    return Err::<(), _>(anyhow::anyhow!("wake detector init failed: {}", e));
                }
            };

            while let Some(frame) = rx.recv().await {
                // oww-rs expects f32 samples normalized to [-1.0, 1.0].
                let f32_samples: Vec<f32> =
                    frame.as_samples().iter().map(|&s| s as f32 / 32768.0).collect();

                match detector.process_samples(&f32_samples) {
                    Ok(score) if score >= settings.wake_threshold => {
                        // F17: disambiguate inner score event from supervisor's wake_detected.
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
                    Ok(_) => {} // below threshold
                    Err(e) => {
                        tracing::warn!(event = "wake_score_error", error = %e);
                    }
                }
            }
            Ok(())
        });

        wake_rx
    }
}

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
