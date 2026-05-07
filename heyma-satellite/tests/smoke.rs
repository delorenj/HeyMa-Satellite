// tests/smoke.rs — Full supervisor integration: stub mic → wake → stream → playback → idle.

extern crate async_trait;

#[path = "../src/audio.rs"]
mod audio;
#[path = "../src/config.rs"]
mod config;
#[path = "../src/gateway.rs"]
mod gateway;
#[path = "../src/utterance.rs"]
mod utterance;
#[path = "../src/wake.rs"]
mod wake;
#[path = "../src/main.rs"]
mod main_mod;

use audio::{AudioFrame, AudioSink, AudioSource};
use anyhow::Result;
use bytes::Bytes;
use futures_util::{SinkExt, StreamExt};
use gateway::{GatewayClient, GatewayFactory};
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio_tungstenite::{accept_async, tungstenite::Message};
use wake::{WakeDetector, WakeEvent, WAKE_SENTINEL};
use config::Settings;

// ---------------------------------------------------------------------------
// Stub AudioSource
// ---------------------------------------------------------------------------

/// Plays back a canned sequence of `AudioFrame` values then closes.
struct StubAudioSource {
    frames: Vec<AudioFrame>,
}

impl StubAudioSource {
    fn new(frames: Vec<AudioFrame>) -> Self {
        StubAudioSource { frames }
    }
}

impl AudioSource for StubAudioSource {
    fn start(self: Box<Self>) -> Result<mpsc::Receiver<AudioFrame>> {
        let (tx, rx) = mpsc::channel(128);
        let frames = self.frames;
        tokio::spawn(async move {
            // Send the canned frames.
            for f in frames {
                if tx.send(f).await.is_err() {
                    return;
                }
            }
            // Then stream silence indefinitely (real mics don't stop).
            // The supervisor shuts down when it receives a shutdown signal,
            // which drops the receiver and causes the send to fail.
            loop {
                let silent = AudioFrame::from_samples(&vec![0i16; 1280]);
                if tx.send(silent).await.is_err() {
                    break;
                }
                // Small yield to avoid busy-looping on the executor.
                tokio::task::yield_now().await;
            }
        });
        Ok(rx)
    }
}

// ---------------------------------------------------------------------------
// Stub AudioSink
// ---------------------------------------------------------------------------

struct CaptureSink {
    pub wav_bytes: Vec<u8>,
    played_tx: mpsc::Sender<Vec<u8>>,
}

impl CaptureSink {
    fn new(played_tx: mpsc::Sender<Vec<u8>>) -> Self {
        CaptureSink {
            wav_bytes: Vec::new(),
            played_tx,
        }
    }
}

impl AudioSink for CaptureSink {
    fn play_wav(&mut self, wav_bytes: Bytes) -> Result<()> {
        let data = wav_bytes.to_vec();
        self.wav_bytes.extend_from_slice(&data);
        // Notify test that playback happened.
        let _ = self.played_tx.try_send(data);
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Stub WakeDetector (re-implements the sentinel logic inline)
// ---------------------------------------------------------------------------

struct SmokeWakeDetector;

impl WakeDetector for SmokeWakeDetector {
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
// Stub GatewayClient — records what it receives, sends back a minimal WAV.
// ---------------------------------------------------------------------------

fn make_wav(pcm_bytes: &[u8]) -> Vec<u8> {
    let data_len = pcm_bytes.len() as u32;
    let file_len = 36 + data_len;
    let mut wav = Vec::with_capacity(44 + pcm_bytes.len());
    wav.extend_from_slice(b"RIFF");
    wav.extend_from_slice(&file_len.to_le_bytes());
    wav.extend_from_slice(b"WAVE");
    wav.extend_from_slice(b"fmt ");
    wav.extend_from_slice(&16u32.to_le_bytes());
    wav.extend_from_slice(&1u16.to_le_bytes());
    wav.extend_from_slice(&1u16.to_le_bytes());
    wav.extend_from_slice(&16_000u32.to_le_bytes());
    wav.extend_from_slice(&32_000u32.to_le_bytes());
    wav.extend_from_slice(&2u16.to_le_bytes());
    wav.extend_from_slice(&16u16.to_le_bytes());
    wav.extend_from_slice(b"data");
    wav.extend_from_slice(&data_len.to_le_bytes());
    wav.extend_from_slice(pcm_bytes);
    wav
}

/// Spawn a full in-process WS fake gateway on a random port.
async fn spawn_fake_gateway() -> (SocketAddr, tokio::task::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let handle = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut ws = accept_async(stream).await.unwrap();

        // Receive hello and extract session_id so we can echo it in ready (F13).
        let msg = ws.next().await.unwrap().unwrap();
        let session_id = if let Message::Text(t) = msg {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap_or_default();
            v["session_id"].as_str().unwrap_or("smoke").to_string()
        } else {
            panic!("expected hello text");
        };

        // Send ready with matching session_id.
        let ready = serde_json::json!({ "type": "ready", "session_id": session_id });
        ws.send(Message::Text(ready.to_string().into())).await.unwrap();

        // Drain PCM + end_of_input.
        loop {
            let msg = ws.next().await.unwrap().unwrap();
            match msg {
                Message::Binary(_) => {}
                Message::Text(t) => {
                    let v: serde_json::Value = serde_json::from_str(&t).unwrap();
                    if v["type"] == "end_of_input" { break; }
                }
                _ => {}
            }
        }

        // Send response.
        let rsp_start = serde_json::json!({ "type": "response_start", "format": "wav" });
        ws.send(Message::Text(rsp_start.to_string().into())).await.unwrap();
        let wav = make_wav(&vec![0u8; 320]);
        ws.send(Message::Binary(wav.into())).await.unwrap();
        let rsp_end = serde_json::json!({ "type": "response_end" });
        ws.send(Message::Text(rsp_end.to_string().into())).await.unwrap();

        // Drain close.
        tokio::time::timeout(std::time::Duration::from_millis(300), async {
            while ws.next().await.is_some() {}
        })
        .await
        .ok();
    });
    (addr, handle)
}

// ---------------------------------------------------------------------------
// Smoke test: full wake → stream → playback cycle
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_full_wake_stream_playback_cycle() {
    // Build canned PCM: 5 silent frames, then the wake sentinel frame,
    // then 10 more silent frames (will trigger end-of-input via silence hold
    // after min_utterance_ms = 80ms is satisfied).
    let mut frames: Vec<AudioFrame> = Vec::new();

    // 5 pre-wake silent frames.
    for _ in 0..5 {
        frames.push(AudioFrame::from_samples(&vec![0i16; 1280]));
    }

    // 1 wake frame.
    let mut wake_samples = vec![0i16; 1280];
    wake_samples[0] = WAKE_SENTINEL;
    frames.push(AudioFrame::from_samples(&wake_samples));

    // 20 silent frames post-wake (20 × 80ms = 1600ms > min=80ms + hold=300ms).
    for _ in 0..20 {
        frames.push(AudioFrame::from_samples(&vec![0i16; 1280]));
    }

    // Spawn fake gateway.
    let (addr, gw_handle) = spawn_fake_gateway().await;

    // Build settings with very short min_utterance_ms so test finishes quickly.
    let settings = Arc::new(Settings {
        gateway_url: format!("ws://127.0.0.1:{}/v1/voice", addr.port()),
        min_utterance_ms: 80,    // 1 frame
        max_utterance_ms: 30_000,
        silence_threshold_db: -40.0,
        sample_rate: 16_000,
        ..Settings::default()
    });

    // Channel for playback notification.
    let (played_tx, mut played_rx) = mpsc::channel::<Vec<u8>>(4);

    let source = Box::new(StubAudioSource::new(frames));
    let sink = Box::new(CaptureSink::new(played_tx));
    let detector = Box::new(SmokeWakeDetector);

    // F1: wrap TungsteniteGateway in a factory closure.
    let gw_url = format!("ws://127.0.0.1:{}/v1/voice", addr.port());
    let gateway_factory: GatewayFactory = Arc::new(move || {
        Box::new(gateway::TungsteniteGateway::new(gw_url.clone()))
    });

    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel::<()>();

    // Run supervisor in a task; shut it down after playback completes.
    let supervisor = tokio::spawn(main_mod::run_supervisor(
        settings,
        source,
        sink,
        detector,
        gateway_factory,
        shutdown_rx,
    ));

    // Wait for at least one playback to happen (timeout = 5 s).
    let wav = tokio::time::timeout(std::time::Duration::from_secs(5), played_rx.recv())
        .await
        .expect("timed out waiting for playback")
        .expect("played channel closed without data");

    // Assert WAV magic bytes.
    assert_eq!(&wav[0..4], b"RIFF", "played WAV must start with RIFF");
    assert_eq!(&wav[8..12], b"WAVE", "played WAV must contain WAVE");

    // Signal shutdown.
    let _ = shutdown_tx.send(());

    // Wait for supervisor + gateway to finish.
    let _ = tokio::time::timeout(std::time::Duration::from_secs(2), supervisor).await;
    let _ = tokio::time::timeout(std::time::Duration::from_secs(2), gw_handle).await;
}

#[tokio::test]
async fn test_clean_shutdown_on_signal() {
    // Supervisor receives shutdown signal before any wake fires; should stop cleanly.
    let settings = Arc::new(Settings::default());
    let source = Box::new(StubAudioSource::new(vec![]));
    let (played_tx, _played_rx) = mpsc::channel::<Vec<u8>>(4);
    let sink = Box::new(CaptureSink::new(played_tx));
    let detector = Box::new(SmokeWakeDetector);

    // F1: use a factory closure.
    let gateway_factory: GatewayFactory = Arc::new(|| Box::new(StubGatewayClient));

    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel::<()>();

    let supervisor = tokio::spawn(main_mod::run_supervisor(
        settings,
        source,
        sink,
        detector,
        gateway_factory,
        shutdown_rx,
    ));

    // Immediately signal shutdown.
    let _ = shutdown_tx.send(());

    let result = tokio::time::timeout(std::time::Duration::from_secs(2), supervisor)
        .await
        .expect("supervisor must stop within 2s after shutdown signal");
    result.expect("supervisor task panicked").expect("supervisor returned error");
}

// ---------------------------------------------------------------------------
// Minimal StubGatewayClient for shutdown-only test
// ---------------------------------------------------------------------------

struct StubGatewayClient;

#[async_trait::async_trait]
impl GatewayClient for StubGatewayClient {
    async fn send_utterance(
        &mut self,
        _session_id: &str,
        _sample_rate: u32,
        _audio_rx: &mut mpsc::Receiver<AudioFrame>,
        sink: &mut dyn AudioSink,
    ) -> Result<usize> {
        let pcm_silence = vec![0u8; 320];
        let wav = make_wav(&pcm_silence);
        sink.play_wav(Bytes::from(wav))?;
        Ok(0)
    }
}
