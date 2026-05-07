// tests/gateway.rs — In-process fake gateway verifying the full wire protocol.

// async_trait must be available for the GatewayClient trait defined in gateway.rs.
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

use audio::{AudioFrame, AudioSink};
use bytes::Bytes;
use futures_util::{SinkExt, StreamExt};
use gateway::{GatewayClient, TungsteniteGateway};
use std::net::SocketAddr;
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio_tungstenite::{accept_async, tungstenite::Message};

// ---------------------------------------------------------------------------
// Stub AudioSink — captures WAV bytes for assertions.
// ---------------------------------------------------------------------------

struct CaptureSink {
    pub captured: Vec<u8>,
}

impl CaptureSink {
    fn new() -> Self {
        CaptureSink { captured: Vec::new() }
    }
}

impl AudioSink for CaptureSink {
    fn play_wav(&mut self, wav_bytes: Bytes) -> anyhow::Result<()> {
        self.captured.extend_from_slice(&wav_bytes);
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Minimal WAV builder for test responses.
// ---------------------------------------------------------------------------

/// Build a minimal 44-byte WAV header + N bytes of silence PCM.
fn make_wav(pcm_bytes: &[u8]) -> Vec<u8> {
    let data_len = pcm_bytes.len() as u32;
    let file_len = 36 + data_len;
    let mut wav = Vec::with_capacity(44 + pcm_bytes.len());
    wav.extend_from_slice(b"RIFF");
    wav.extend_from_slice(&file_len.to_le_bytes());
    wav.extend_from_slice(b"WAVE");
    wav.extend_from_slice(b"fmt ");
    wav.extend_from_slice(&16u32.to_le_bytes()); // chunk size
    wav.extend_from_slice(&1u16.to_le_bytes());  // PCM
    wav.extend_from_slice(&1u16.to_le_bytes());  // mono
    wav.extend_from_slice(&16_000u32.to_le_bytes()); // sample rate
    wav.extend_from_slice(&32_000u32.to_le_bytes()); // byte rate
    wav.extend_from_slice(&2u16.to_le_bytes());  // block align
    wav.extend_from_slice(&16u16.to_le_bytes()); // bits per sample
    wav.extend_from_slice(b"data");
    wav.extend_from_slice(&data_len.to_le_bytes());
    wav.extend_from_slice(pcm_bytes);
    wav
}

// ---------------------------------------------------------------------------
// Fake gateway server.
// ---------------------------------------------------------------------------

/// Spin up a fake gateway on a random port, drive the protocol, and collect
/// what the client sent. Returns the server address and a task handle.
async fn spawn_fake_gateway(
    expected_pcm_frames: usize,
    wav_chunk_count: usize,
) -> (SocketAddr, tokio::task::JoinHandle<FakeGatewayRecord>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();

    let handle = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut ws = accept_async(stream).await.unwrap();
        let mut record = FakeGatewayRecord::default();

        // 1. Receive hello.
        let msg = ws.next().await.unwrap().unwrap();
        if let Message::Text(t) = msg {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "hello");
            record.hello_session_id = v["session_id"].as_str().unwrap().to_string();
            record.hello_sample_rate = v["sample_rate"].as_u64().unwrap() as u32;
            record.hello_encoding = v["encoding"].as_str().unwrap().to_string();
            record.hello_channels = v["channels"].as_u64().unwrap() as u8;
            record.hello_client = v["client"].as_str().unwrap().to_string();
        } else {
            panic!("expected text hello, got {:?}", msg);
        }

        // 2. Send ready.
        let ready = serde_json::json!({
            "type": "ready",
            "session_id": record.hello_session_id,
        });
        ws.send(Message::Text(ready.to_string().into()))
            .await
            .unwrap();

        // 3. Receive binary frames + end_of_input.
        loop {
            let msg = ws.next().await.unwrap().unwrap();
            match msg {
                Message::Binary(b) => {
                    record.pcm_frame_count += 1;
                    record.pcm_total_bytes += b.len();
                }
                Message::Text(t) => {
                    let v: serde_json::Value = serde_json::from_str(&t).unwrap();
                    if v["type"] == "end_of_input" {
                        record.end_of_input_received = true;
                        break;
                    } else if v["type"] == "close" {
                        record.close_received = true;
                        break;
                    }
                }
                _ => {}
            }
        }
        assert!(record.end_of_input_received, "end_of_input never received");

        // 4. Send response_start.
        let rsp_start = serde_json::json!({ "type": "response_start", "format": "wav" });
        ws.send(Message::Text(rsp_start.to_string().into()))
            .await
            .unwrap();

        // 5. Send WAV in chunks.
        let pcm_silence = vec![0u8; 320]; // 160 samples = 10 ms
        let wav_bytes = make_wav(&pcm_silence);
        let chunk_size = (wav_bytes.len() + wav_chunk_count - 1) / wav_chunk_count;
        for chunk in wav_bytes.chunks(chunk_size) {
            ws.send(Message::Binary(chunk.to_vec().into())).await.unwrap();
            record.wav_total_bytes += chunk.len();
        }

        // 6. Send response_end.
        let rsp_end = serde_json::json!({ "type": "response_end" });
        ws.send(Message::Text(rsp_end.to_string().into()))
            .await
            .unwrap();

        // 7. Wait for close from client (best-effort, don't block).
        tokio::time::timeout(std::time::Duration::from_millis(200), async {
            while let Some(Ok(msg)) = ws.next().await {
                if let Message::Text(t) = msg {
                    let v: serde_json::Value = serde_json::from_str(&t).unwrap_or_default();
                    if v["type"] == "close" {
                        record.close_received = true;
                        break;
                    }
                }
            }
        })
        .await
        .ok();

        record
    });

    (addr, handle)
}

#[derive(Default)]
struct FakeGatewayRecord {
    hello_session_id: String,
    hello_sample_rate: u32,
    hello_encoding: String,
    hello_channels: u8,
    hello_client: String,
    pcm_frame_count: usize,
    pcm_total_bytes: usize,
    end_of_input_received: bool,
    close_received: bool,
    wav_total_bytes: usize,
}

// ---------------------------------------------------------------------------
// Helper: build N audio frames for the utterance channel.
// ---------------------------------------------------------------------------

fn build_audio_frames(n: usize) -> Vec<AudioFrame> {
    (0..n)
        .map(|_| {
            let samples = vec![100i16; 1280]; // non-zero PCM
            AudioFrame::from_samples(&samples)
        })
        .collect()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_hello_fields_are_correct() {
    let frame_count = 3;
    let (addr, server_handle) = spawn_fake_gateway(frame_count, 1).await;

    let url = format!("ws://127.0.0.1:{}", addr.port());
    let mut client = TungsteniteGateway::new(url);
    let mut sink = CaptureSink::new();

    let (audio_tx, mut audio_rx) = mpsc::channel::<AudioFrame>(16);
    let frames = build_audio_frames(frame_count);
    for f in frames {
        audio_tx.send(f).await.unwrap();
    }
    drop(audio_tx); // signal end of utterance

    let session_id = "test-session-123";
    client
        .send_utterance(session_id, 16_000, &mut audio_rx, &mut sink)
        .await
        .expect("send_utterance failed");

    let record = server_handle.await.unwrap();
    assert_eq!(record.hello_session_id, session_id);
    assert_eq!(record.hello_sample_rate, 16_000);
    assert_eq!(record.hello_encoding, "pcm_s16le");
    assert_eq!(record.hello_channels, 1);
    assert_eq!(record.hello_client, "heyma");
}

#[tokio::test]
async fn test_pcm_frame_count_forwarded() {
    let frame_count = 5;
    let (addr, server_handle) = spawn_fake_gateway(frame_count, 1).await;

    let url = format!("ws://127.0.0.1:{}", addr.port());
    let mut client = TungsteniteGateway::new(url);
    let mut sink = CaptureSink::new();

    let (audio_tx, mut audio_rx) = mpsc::channel::<AudioFrame>(32);
    for f in build_audio_frames(frame_count) {
        audio_tx.send(f).await.unwrap();
    }
    drop(audio_tx);

    let frames_sent = client
        .send_utterance("session-2", 16_000, &mut audio_rx, &mut sink)
        .await
        .expect("send_utterance");

    let record = server_handle.await.unwrap();
    assert_eq!(record.pcm_frame_count, frame_count);
    assert_eq!(frames_sent, frame_count);
}

#[tokio::test]
async fn test_pcm_frame_bytes_correct_size() {
    // Each frame = 1280 samples × 2 bytes = 2560 bytes.
    let frame_count = 4;
    let (addr, server_handle) = spawn_fake_gateway(frame_count, 1).await;

    let url = format!("ws://127.0.0.1:{}", addr.port());
    let mut client = TungsteniteGateway::new(url);
    let mut sink = CaptureSink::new();

    let (audio_tx, mut audio_rx) = mpsc::channel::<AudioFrame>(16);
    for f in build_audio_frames(frame_count) {
        audio_tx.send(f).await.unwrap();
    }
    drop(audio_tx);

    client
        .send_utterance("session-3", 16_000, &mut audio_rx, &mut sink)
        .await
        .expect("send_utterance");

    let record = server_handle.await.unwrap();
    assert_eq!(record.pcm_total_bytes, frame_count * 2560);
}

#[tokio::test]
async fn test_end_of_input_sent() {
    let (addr, server_handle) = spawn_fake_gateway(2, 1).await;

    let url = format!("ws://127.0.0.1:{}", addr.port());
    let mut client = TungsteniteGateway::new(url);
    let mut sink = CaptureSink::new();

    let (audio_tx, mut audio_rx) = mpsc::channel::<AudioFrame>(8);
    for f in build_audio_frames(2) {
        audio_tx.send(f).await.unwrap();
    }
    drop(audio_tx);

    client
        .send_utterance("session-4", 16_000, &mut audio_rx, &mut sink)
        .await
        .expect("send_utterance");

    let record = server_handle.await.unwrap();
    assert!(record.end_of_input_received, "end_of_input not received by server");
}

#[tokio::test]
async fn test_wav_assembled_correctly() {
    // Send WAV in 3 chunks; client should assemble them into the complete buffer.
    let (addr, server_handle) = spawn_fake_gateway(1, 3).await;

    let url = format!("ws://127.0.0.1:{}", addr.port());
    let mut client = TungsteniteGateway::new(url);
    let mut sink = CaptureSink::new();

    let (audio_tx, mut audio_rx) = mpsc::channel::<AudioFrame>(8);
    audio_tx.send(build_audio_frames(1).remove(0)).await.unwrap();
    drop(audio_tx);

    client
        .send_utterance("session-5", 16_000, &mut audio_rx, &mut sink)
        .await
        .expect("send_utterance");

    let record = server_handle.await.unwrap();

    // Verify the sink received the complete WAV (== what the server sent).
    assert_eq!(
        sink.captured.len(),
        record.wav_total_bytes,
        "assembled WAV byte count mismatch"
    );
    // Verify WAV header magic.
    assert_eq!(&sink.captured[0..4], b"RIFF");
    assert_eq!(&sink.captured[8..12], b"WAVE");
}

#[tokio::test]
async fn test_response_end_consumed() {
    // The client must consume response_end cleanly without leaving messages unread.
    let (addr, server_handle) = spawn_fake_gateway(2, 2).await;

    let url = format!("ws://127.0.0.1:{}", addr.port());
    let mut client = TungsteniteGateway::new(url);
    let mut sink = CaptureSink::new();

    let (audio_tx, mut audio_rx) = mpsc::channel::<AudioFrame>(8);
    for f in build_audio_frames(2) {
        audio_tx.send(f).await.unwrap();
    }
    drop(audio_tx);

    // This would hang or error if response_end wasn't handled.
    client
        .send_utterance("session-6", 16_000, &mut audio_rx, &mut sink)
        .await
        .expect("send_utterance must complete without error");

    // Server completes cleanly.
    server_handle.await.unwrap();
}

// ---------------------------------------------------------------------------
// F18: AC #5 -- gateway disconnects mid-utterance (drops WS without response_start)
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_gateway_disconnect_mid_utterance_returns_error() {
    // Fake server: accepts hello, sends ready, receives 2-3 binary PCM frames,
    // then closes the WebSocket abruptly without sending response_start.
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();

    let server_handle = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut ws = accept_async(stream).await.unwrap();

        // 1. Receive hello.
        let msg = ws.next().await.unwrap().unwrap();
        let session_id = if let Message::Text(t) = msg {
            let v: serde_json::Value = serde_json::from_str(&t).unwrap();
            assert_eq!(v["type"], "hello");
            v["session_id"].as_str().unwrap().to_string()
        } else {
            panic!("expected hello text");
        };

        // 2. Send ready.
        let ready = serde_json::json!({ "type": "ready", "session_id": session_id });
        ws.send(Message::Text(ready.to_string().into())).await.unwrap();

        // 3. Receive 2-3 binary PCM frames, then close abruptly.
        let mut received = 0usize;
        while let Some(Ok(msg)) = ws.next().await {
            match msg {
                Message::Binary(_) => {
                    received += 1;
                    if received >= 2 {
                        break;
                    }
                }
                Message::Text(_) => break,
                _ => {}
            }
        }
        // Close the underlying stream without sending response_start.
        drop(ws);
    });

    let url = format!("ws://127.0.0.1:{}", addr.port());
    let mut client = TungsteniteGateway::new(url);
    let mut sink = CaptureSink::new();

    let (audio_tx, mut audio_rx) = mpsc::channel::<AudioFrame>(16);
    // Send frames continuously; the server will drop after 2.
    for f in build_audio_frames(10) {
        // Ignore send errors -- channel may close as server drops connection.
        let _ = audio_tx.send(f).await;
    }
    drop(audio_tx);

    // send_utterance should return an error because the gateway disconnected
    // mid-utterance without completing the protocol.
    let result = client
        .send_utterance("session-disconnect", 16_000, &mut audio_rx, &mut sink)
        .await;

    assert!(
        result.is_err(),
        "send_utterance must return Err when gateway disconnects mid-utterance"
    );

    // Server task should also have completed.
    server_handle.await.unwrap();
}
