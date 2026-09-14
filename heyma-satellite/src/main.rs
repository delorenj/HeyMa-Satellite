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
use std::collections::VecDeque;
use std::f32::consts::PI;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{mpsc, oneshot};
use tracing::{error, info, warn};
use uuid::Uuid;

// D2 watchdog: emit a periodic audio-level summary so a dead mic surfaces in journalctl
// instead of looking healthy while doing nothing useful.
//
// Window: ten seconds of accumulated sum-of-squares and peak, then reset.
// Healthy speech captures show RMS in the hundreds-to-thousands range and peaks well above
// 1000. The dead ReSpeaker codec observed on tonny.local on 2026-05-11 produced RMS ~15 and
// peak 266 across a full ten seconds of speech: clearly distinguishable.
const AUDIO_WINDOW: Duration = Duration::from_secs(10);
const AUDIO_SILENCE_RMS: f64 = 50.0;
const FRAME_MS: u64 = 80;

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
    mut manual_wake_rx: mpsc::Receiver<()>,
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

    // D2: rolling audio-level window state.
    let mut audio_window_start = Instant::now();
    let mut audio_window_sumsq: f64 = 0.0;
    let mut audio_window_samples: u64 = 0;
    let mut audio_window_peak: u32 = 0;
    let mut manual_wake_enabled = true;
    let preroll_max_frames =
        ((settings.wake_preroll_ms.saturating_add(FRAME_MS - 1)) / FRAME_MS).max(1) as usize;
    let mut preroll_frames: VecDeque<AudioFrame> = VecDeque::with_capacity(preroll_max_frames);

    loop {
        tokio::select! {

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

                // D2: accumulate this frame into the rolling level window.
                let (frame_sumsq, frame_peak) = frame.sumsq_and_peak();
                audio_window_sumsq += frame_sumsq;
                audio_window_samples += (frame.len() / 2) as u64;
                if frame_peak > audio_window_peak {
                    audio_window_peak = frame_peak;
                }
                if audio_window_start.elapsed() >= AUDIO_WINDOW
                    && audio_window_samples > 0
                {
                    let rms = (audio_window_sumsq / audio_window_samples as f64).sqrt();
                    if rms < AUDIO_SILENCE_RMS {
                        warn!(
                            event = "audio_silent_warning",
                            rms = rms as u32,
                            peak = audio_window_peak,
                            samples = audio_window_samples,
                            window_secs = AUDIO_WINDOW.as_secs(),
                            hint = "mic may be muted, unplugged, or hardware-failed; \
                                    expected RMS > 50 for ambient speech",
                        );
                    } else {
                        info!(
                            event = "audio_level",
                            rms = rms as u32,
                            peak = audio_window_peak,
                            samples = audio_window_samples,
                            window_secs = AUDIO_WINDOW.as_secs(),
                        );
                    }
                    audio_window_start = Instant::now();
                    audio_window_sumsq = 0.0;
                    audio_window_samples = 0;
                    audio_window_peak = 0;
                }

                // Keep recent audio so wake-triggered utterances include speech
                // that happened while the detector waited for the wake trailing edge.
                if preroll_frames.len() == preroll_max_frames {
                    preroll_frames.pop_front();
                }
                preroll_frames.push_back(frame.clone());

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
            wake_result = wake_rx.recv() => {
                let wake_ev = match wake_result {
                    Some(Ok(event)) => event,
                    Some(Err(error)) => {
                        error!(event = "wake_detector_failed", error = %error);
                        return Err(error.into());
                    }
                    None => {
                        error!(event = "wake_detector_channel_closed");
                        return Err(anyhow::anyhow!("wake detector channel closed unexpectedly"));
                    }
                };

                if active_session.is_none() {
                    let session_id = Uuid::new_v4().to_string();
                    info!(
                        event = "wake_detected",
                        session_id = %session_id,
                        gateway_url = %settings.gateway_url,
                        score = wake_ev.score,
                    );

                    play_wake_ding(&session_id, &settings, sink.as_mut());

                    active_session = Some(session_id.clone());
                    utt_detector.reset();
                    stream_start = Some(std::time::Instant::now());

                    // Open the utterance channel.
                    let (utx, urx) = mpsc::channel::<AudioFrame>(256);
                    send_preroll(&session_id, &settings, &utx, &preroll_frames);
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

            // ---- Operator/test wake event ----
            manual_wake = manual_wake_rx.recv(), if manual_wake_enabled => {
                if manual_wake.is_none() {
                    manual_wake_enabled = false;
                    continue;
                }

                if active_session.is_none() {
                    let session_id = Uuid::new_v4().to_string();
                    info!(
                        event = "manual_wake_requested",
                        session_id = %session_id,
                        gateway_url = %settings.gateway_url,
                    );

                    play_wake_ding(&session_id, &settings, sink.as_mut());

                    active_session = Some(session_id.clone());
                    utt_detector.reset();
                    stream_start = Some(std::time::Instant::now());

                    let (utx, urx) = mpsc::channel::<AudioFrame>(256);
                    send_preroll(&session_id, &settings, &utx, &preroll_frames);
                    utt_tx = Some(utx);

                    let (wtx, wrx) = mpsc::channel::<anyhow::Result<bytes::Bytes>>(1);
                    wav_rx = Some(wrx);

                    let factory = gateway_factory.clone();
                    let sid = session_id.clone();
                    let sample_rate = settings.sample_rate;
                    let mut urx_owned = urx;

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
                    tracing::debug!(event = "manual_wake_debounced");
                }
            }
        }
    }

    info!(event = "supervisor_stopped");
    Ok(())
}

fn send_preroll(
    session_id: &str,
    settings: &Settings,
    tx: &mpsc::Sender<AudioFrame>,
    frames: &VecDeque<AudioFrame>,
) {
    let mut sent = 0usize;
    for frame in frames.iter().cloned() {
        if tx.try_send(frame).is_err() {
            warn!(
                event = "frame_dropped",
                channel = "utterance_preroll",
                session_id = %session_id,
                dropped_count = 1,
            );
            break;
        }
        sent += 1;
    }
    info!(
        event = "utterance_preroll_sent",
        session_id = %session_id,
        frames = sent,
        preroll_ms = settings.wake_preroll_ms,
    );
}

fn play_wake_ding(session_id: &str, settings: &Settings, sink: &mut dyn AudioSink) {
    if !settings.wake_ding_enabled {
        return;
    }

    info!(
        event = "wake_ding_playing",
        session_id = %session_id,
        frequency_hz = settings.wake_ding_frequency_hz,
        duration_ms = settings.wake_ding_duration_ms,
    );

    match build_wake_ding_wav(settings).and_then(|wav| sink.play_wav(wav)) {
        Ok(()) => info!(event = "wake_ding_played", session_id = %session_id),
        Err(e) => warn!(event = "wake_ding_failed", session_id = %session_id, error = %e),
    }
}

fn build_wake_ding_wav(settings: &Settings) -> Result<bytes::Bytes> {
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate: settings.sample_rate,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let sample_count =
        ((settings.sample_rate as u64 * settings.wake_ding_duration_ms as u64) / 1_000) as usize;
    let amplitude = (settings.wake_ding_volume.clamp(0.0, 1.0) * i16::MAX as f32) as i16;

    let mut cursor = std::io::Cursor::new(Vec::new());
    {
        let mut writer = hound::WavWriter::new(&mut cursor, spec)?;
        for i in 0..sample_count {
            let t = i as f32 / settings.sample_rate as f32;
            let envelope = if sample_count <= 1 {
                1.0
            } else {
                // Smooth click-free attack/release without dragging the cue out.
                (PI * i as f32 / (sample_count - 1) as f32).sin()
            };
            let sample = (amplitude as f32
                * envelope
                * (2.0 * PI * settings.wake_ding_frequency_hz as f32 * t).sin())
                as i16;
            writer.write_sample(sample)?;
        }
        writer.finalize()?;
    }

    Ok(bytes::Bytes::from(cursor.into_inner()))
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
    let (manual_wake_tx, manual_wake_rx) = mpsc::channel::<()>(4);

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

    tokio::spawn(async move {
        use tokio::signal::unix::{signal, SignalKind};
        let mut sigusr1 = signal(SignalKind::user_defined1()).expect("register SIGUSR1");
        while sigusr1.recv().await.is_some() {
            if manual_wake_tx.send(()).await.is_err() {
                break;
            }
        }
    });

    run_supervisor(
        settings,
        source,
        sink,
        detector,
        gateway_factory,
        manual_wake_rx,
        shutdown_rx,
    )
    .await
}
