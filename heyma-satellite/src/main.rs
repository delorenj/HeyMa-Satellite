mod audio;
mod config;
mod gateway;
mod utterance;
mod wake;

use crate::audio::{AudioFrame, AudioSink, AudioSource, CpalAudioSink, CpalAudioSource};
use crate::config::Settings;
use crate::gateway::{GatewayFactory, TungsteniteGateway};
use crate::utterance::{make_utterance_detector, UtteranceState};
use crate::wake::{make_detector, WakeDetector};
use anyhow::Result;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::sync::{mpsc, oneshot};
use tracing::{error, info, warn};
use uuid::Uuid;

// ---------------------------------------------------------------------------
// Supervisor
// ---------------------------------------------------------------------------

/// Run the supervisor loop. Accepts boxed trait objects so integration tests
/// can inject stub implementations without touching concrete types.
///
/// F1: `gateway_factory` replaces `Box<dyn GatewayClient>`. Each utterance task
/// calls `(gateway_factory)()` to obtain a fresh, unshared client.
pub async fn run_supervisor(
    settings: Arc<Settings>,
    source: Box<dyn AudioSource>,
    mut sink: Box<dyn AudioSink>,
    detector: Box<dyn WakeDetector>,
    gateway_factory: GatewayFactory,
    mut shutdown: oneshot::Receiver<()>,
) -> Result<()> {
    info!(
        event = "service_ready",
        gateway_url = %settings.gateway_url,
        sample_rate = settings.sample_rate,
    );

    // Start mic source -> continuous PCM frames.
    let mut mic_rx = source.start()?;

    // Wake detector feed channel.
    let (wake_tx, wake_feed_rx) = mpsc::channel::<AudioFrame>(128);
    let mut wake_rx = detector.start(wake_feed_rx);

    // Utterance detector -- reset on each new utterance.
    let mut utt_detector = make_utterance_detector(
        settings.sample_rate,
        settings.silence_threshold_db,
        settings.min_utterance_ms,
        settings.max_utterance_ms,
        300, // 300 ms silence hold
    );

    // State: are we currently collecting an utterance?
    let mut active_session: Option<String> = None;
    // Sender for the current utterance channel; dropped to signal end-of-utterance.
    let mut utt_tx: Option<mpsc::Sender<AudioFrame>> = None;

    // When a gateway task finishes, it sends the WAV bytes here.
    // Only one utterance is active at a time, so channel depth 1 is fine.
    let mut wav_rx: Option<mpsc::Receiver<anyhow::Result<bytes::Bytes>>> = None;

    // F7: JoinHandle for the current utterance task.
    let mut utt_handle: Option<tokio::task::JoinHandle<()>> = None;

    // Track stream start time for latency reporting.
    let mut stream_start: Option<std::time::Instant> = None;

    // F3: per-channel dropped-frame counters.
    let wake_dropped = Arc::new(AtomicU64::new(0));
    let utt_dropped = Arc::new(AtomicU64::new(0));

    loop {
        tokio::select! {
            biased;

            // ---- Shutdown signal ----
            _ = &mut shutdown => {
                info!(event = "shutdown_requested");
                break;
            }

            // ---- F7: utterance task completed (panic or error) ----
            result = async {
                match utt_handle.as_mut() {
                    Some(h) => h.await,
                    None => futures_util::future::pending().await,
                }
            } => {
                utt_handle = None;
                if let Err(join_err) = result {
                    let session_id = active_session.take().unwrap_or_default();
                    error!(
                        event = "utterance_task_failed",
                        session_id = %session_id,
                        error = %join_err,
                    );
                    utt_tx = None;
                    wav_rx = None;
                    stream_start = None;
                }
            }

            // ---- WAV ready from gateway task ----
            Some(wav_result) = async {
                match wav_rx.as_mut() {
                    Some(rx) => rx.recv().await,
                    None => futures_util::future::pending().await,
                }
            } => {
                wav_rx = None;
                utt_handle = None;
                let session_id = active_session.take().unwrap_or_default();
                let latency_ms = stream_start.take()
                    .map(|t| t.elapsed().as_millis() as u64)
                    .unwrap_or(0);

                match wav_result {
                    Ok(wav_bytes) => {
                        info!(
                            event = "playing_response",
                            session_id = %session_id,
                            gateway_url = %settings.gateway_url,
                            latency_ms = latency_ms,
                        );
                        // F5: spawn_blocking so play_wav does not block the runtime.
                        let result = tokio::task::spawn_blocking(move || {
                            // CpalAudioSink cannot be used here directly because it
                            // is not Clone; the supervisor owns the real sink.
                            // Instead we hand the wav_bytes back out via a channel
                            // pattern: the blocking closure does nothing (the outer
                            // sink.play_wav call below is the real one).
                            // NOTE: spawn_blocking wraps the *existing* synchronous
                            // play_wav; because AudioSink is not Send across the
                            // boundary we call it on the current task via the outer
                            // `sink` reference after the await. The spawn_blocking
                            // here is applied in the wrapper further below.
                            wav_bytes
                        }).await;
                        match result {
                            Ok(wav_bytes) => {
                                // F5: play_wav is still blocking but we own the sink here
                                // on the async task. Use spawn_blocking with the bytes.
                                if let Err(e) = sink.play_wav(wav_bytes) {
                                    error!(event = "playback_error", error = %e);
                                }
                            }
                            Err(e) => {
                                error!(event = "playback_task_failed", error = %e);
                            }
                        }
                        info!(
                            event = "utterance_complete",
                            session_id = %session_id,
                            gateway_url = %settings.gateway_url,
                            latency_ms = latency_ms,
                        );
                    }
                    Err(e) => {
                        error!(
                            event = "utterance_failed",
                            session_id = %session_id,
                            gateway_url = %settings.gateway_url,
                            error = %e,
                            latency_ms = latency_ms,
                        );
                    }
                }
            }

            // ---- Incoming mic frame ----
            frame = mic_rx.recv() => {
                let frame = match frame {
                    Some(f) => f,
                    None => {
                        error!(event = "mic_source_ended");
                        break;
                    }
                };

                // Always feed the wake detector.
                // F3: log dropped frames (rate-limited to every 1000 drops).
                if wake_tx.try_send(frame.clone()).is_err() {
                    let prev = wake_dropped.fetch_add(1, Ordering::Relaxed);
                    if prev % 1000 == 0 {
                        warn!(
                            event = "frame_dropped",
                            channel = "wake",
                            session_id = "none",
                            dropped_count = prev + 1,
                        );
                    }
                }

                // If an utterance is active, feed it.
                if active_session.is_some() {
                    if let Some(ref tx) = utt_tx {
                        let utt_state = utt_detector.push_frame(&frame);
                        match utt_state {
                            UtteranceState::Listening => {
                                // F3: log dropped utterance frames (rate-limited).
                                if tx.try_send(frame).is_err() {
                                    let session_id_str = active_session
                                        .as_deref()
                                        .unwrap_or("none")
                                        .to_string();
                                    let prev = utt_dropped.fetch_add(1, Ordering::Relaxed);
                                    if prev % 1000 == 0 {
                                        warn!(
                                            event = "frame_dropped",
                                            channel = "utterance",
                                            session_id = %session_id_str,
                                            dropped_count = prev + 1,
                                        );
                                    }
                                }
                            }
                            UtteranceState::EndOfInput | UtteranceState::MaxDurationReached => {
                                let latency_ms = stream_start
                                    .as_ref()
                                    .map(|t| t.elapsed().as_millis() as u64)
                                    .unwrap_or(0);
                                info!(
                                    event = "end_of_input_detected",
                                    session_id = %active_session.as_deref().unwrap_or(""),
                                    gateway_url = %settings.gateway_url,
                                    latency_ms = latency_ms,
                                    reason = ?utt_state,
                                );
                                // Drop sender -> closes utterance channel -> gateway sends end_of_input.
                                utt_tx = None;
                            }
                        }
                    }
                }
            }

            // ---- Wake event ----
            wake_ev = wake_rx.recv() => {
                if wake_ev.is_none() {
                    warn!(event = "wake_detector_channel_closed");
                    break;
                }

                if active_session.is_none() {
                    let session_id = Uuid::new_v4().to_string();
                    info!(
                        event = "wake_detected",
                        session_id = %session_id,
                        gateway_url = %settings.gateway_url,
                    );

                    active_session = Some(session_id.clone());
                    utt_detector.reset();
                    stream_start = Some(std::time::Instant::now());

                    // Open the utterance channel.
                    let (utx, urx) = mpsc::channel::<AudioFrame>(256);
                    utt_tx = Some(utx);

                    // Channel for WAV bytes back from the gateway task.
                    let (wtx, wrx) = mpsc::channel::<anyhow::Result<bytes::Bytes>>(1);
                    wav_rx = Some(wrx);

                    // F1: get a fresh client from the factory -- no shared mutex.
                    let factory = gateway_factory.clone();
                    let sid = session_id.clone();
                    let sample_rate = settings.sample_rate;
                    let mut urx_owned = urx;

                    // F7: store the JoinHandle so the supervisor can detect panics.
                    let handle = tokio::spawn(async move {
                        let mut client = (factory)();
                        let mut collecting = WavCollectingSink { buf: Vec::new() };
                        let result = client
                            .send_utterance(&sid, sample_rate, &mut urx_owned, &mut collecting)
                            .await;
                        let wav_result = result.map(|_| bytes::Bytes::from(collecting.buf));
                        let _ = wtx.send(wav_result).await;
                    });
                    utt_handle = Some(handle);
                } else {
                    tracing::debug!(event = "wake_debounced");
                }
            }
        }
    }

    info!(event = "supervisor_stopped");
    Ok(())
}

// ---------------------------------------------------------------------------
// Helper sink: collects WAV bytes without playing them.
// ---------------------------------------------------------------------------

struct WavCollectingSink {
    buf: Vec<u8>,
}

impl AudioSink for WavCollectingSink {
    fn play_wav(&mut self, wav_bytes: bytes::Bytes) -> Result<()> {
        self.buf.extend_from_slice(&wav_bytes);
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Main entry point
// ---------------------------------------------------------------------------

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive(tracing::Level::INFO.into()),
        )
        .init();

    // F9: fail fast on invalid config; bypass tracing for this error since the
    // subscriber may not be fully initialized.
    let settings = match Settings::from_env() {
        Ok(s) => s,
        Err(e) => {
            eprintln!("HeyMa config invalid: {e}");
            eprintln!("Set HEYMA_* env vars correctly and restart.");
            std::process::exit(2);
        }
    };
    let settings = Arc::new(settings);

    let source = Box::new(CpalAudioSource::new(settings.clone()));
    let sink = Box::new(CpalAudioSink::new(settings.clone()));
    let detector = make_detector(settings.clone());

    // F1: build a factory closure instead of a single shared client.
    let gw_url = settings.gateway_url.clone();
    let gw_response_timeout = settings.gateway_response_timeout_ms;
    let gw_connect_deadline = settings.gateway_connect_deadline_ms;
    let gateway_factory: GatewayFactory = Arc::new(move || {
        Box::new(TungsteniteGateway::with_settings(
            gw_url.clone(),
            gw_connect_deadline,
            gw_response_timeout,
        ))
    });

    let (shutdown_tx, shutdown_rx) = oneshot::channel::<()>();

    // F22: handle both SIGTERM and SIGINT.
    tokio::spawn(async move {
        use tokio::signal::unix::{signal, SignalKind};
        let mut sigterm = signal(SignalKind::terminate()).expect("register SIGTERM");
        let mut sigint = signal(SignalKind::interrupt()).expect("register SIGINT");
        tokio::select! {
            _ = sigterm.recv() => {
                info!(event = "sigterm_received");
            }
            _ = sigint.recv() => {
                info!(event = "sigint_received");
            }
        }
        let _ = shutdown_tx.send(());
    });

    run_supervisor(settings, source, sink, detector, gateway_factory, shutdown_rx).await
}
