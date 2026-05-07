use crate::audio::{AudioFrame, AudioSink};
use anyhow::{bail, Context, Result};
use async_trait::async_trait;
use bytes::Bytes;
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use std::time::{Duration, Instant};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio_tungstenite::{connect_async, tungstenite::Message, MaybeTlsStream, WebSocketStream};

// ---------------------------------------------------------------------------
// Wire protocol messages
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ClientMessage {
    Hello {
        session_id: String,
        sample_rate: u32,
        encoding: &'static str,
        channels: u8,
        client: &'static str,
        version: &'static str,
    },
    EndOfInput,
    Close,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
#[allow(dead_code)]
pub enum ServerMessage {
    Ready {
        session_id: String,
    },
    ResponseStart {
        format: String,
    },
    ResponseEnd,
    Error {
        code: String,
        message: String,
    },
    Close,
}

// ---------------------------------------------------------------------------
// GatewayClient trait
// ---------------------------------------------------------------------------

/// Trait seam for the gateway transport. `TungsteniteGateway` is the production
/// impl; tests provide a stub over an in-process WS server.
///
/// Uses `async_trait` so the trait is dyn-compatible (vtable-safe).
#[async_trait]
pub trait GatewayClient: Send + 'static {
    /// Perform one full utterance round-trip:
    ///   connect -> hello -> ready -> stream PCM -> end_of_input
    ///   -> response_start -> collect WAV -> response_end
    ///   -> hand WAV to sink.
    ///
    /// Returns the number of PCM binary frames sent.
    async fn send_utterance(
        &mut self,
        session_id: &str,
        sample_rate: u32,
        audio_rx: &mut mpsc::Receiver<AudioFrame>,
        sink: &mut dyn AudioSink,
    ) -> Result<usize>;
}

// ---------------------------------------------------------------------------
// F12: cap on accumulated WAV bytes
// ---------------------------------------------------------------------------

const MAX_WAV_BYTES: usize = 10 * 1024 * 1024;

// ---------------------------------------------------------------------------
// GatewayFactory type alias (F1)
// ---------------------------------------------------------------------------

/// Factory that produces a fresh `GatewayClient` per utterance, eliminating
/// shared mutable state between concurrent utterance tasks.
pub type GatewayFactory = std::sync::Arc<dyn Fn() -> Box<dyn GatewayClient> + Send + Sync>;

// ---------------------------------------------------------------------------
// TungsteniteGateway -- production implementation
// ---------------------------------------------------------------------------

pub struct TungsteniteGateway {
    gateway_url: String,
    connect_deadline_ms: u64,
    response_timeout_ms: u64,
}

impl TungsteniteGateway {
    pub fn new(gateway_url: impl Into<String>) -> Self {
        TungsteniteGateway {
            gateway_url: gateway_url.into(),
            connect_deadline_ms: 60_000,
            response_timeout_ms: 30_000,
        }
    }

    /// Construct with explicit timeout settings (used by supervisor with Settings).
    pub fn with_settings(
        gateway_url: impl Into<String>,
        connect_deadline_ms: u64,
        response_timeout_ms: u64,
    ) -> Self {
        TungsteniteGateway {
            gateway_url: gateway_url.into(),
            connect_deadline_ms,
            response_timeout_ms,
        }
    }

    // F2: wrap connect_async with a 5 s per-attempt timeout.
    async fn connect_with_timeout(
        &self,
    ) -> Result<WebSocketStream<MaybeTlsStream<TcpStream>>> {
        let fut = connect_async(&self.gateway_url);
        match tokio::time::timeout(Duration::from_secs(5), fut).await {
            Ok(Ok((ws_stream, _))) => Ok(ws_stream),
            Ok(Err(e)) => Err(e).with_context(|| format!("connect to {}", self.gateway_url)),
            Err(_) => bail!("connect timeout to {}", self.gateway_url),
        }
    }
}

#[async_trait]
impl GatewayClient for TungsteniteGateway {
    async fn send_utterance(
        &mut self,
        session_id: &str,
        sample_rate: u32,
        audio_rx: &mut mpsc::Receiver<AudioFrame>,
        sink: &mut dyn AudioSink,
    ) -> Result<usize> {
        // F15: track wake-to-now latency for gateway_unreachable log.
        let wake_started_at = Instant::now();

        // F6: overall connect deadline.
        let connect_deadline = Instant::now()
            + Duration::from_millis(self.connect_deadline_ms);

        // ---- Exponential backoff connect loop ----
        let mut attempt = 0u32;
        let mut ws = loop {
            // F6: check overall deadline before each attempt.
            if Instant::now() >= connect_deadline {
                bail!(
                    "gateway connect deadline exceeded after {} attempts (deadline {}ms)",
                    attempt,
                    self.connect_deadline_ms
                );
            }

            match self.connect_with_timeout().await {
                Ok(ws) => break ws,
                Err(e) => {
                    let base_delay_ms: u64 = 1000 * (1u64 << attempt.min(5));
                    let delay_ms = base_delay_ms.min(30_000);

                    // F15: include session_id and latency_ms in retry log.
                    tracing::warn!(
                        event = "gateway_unreachable",
                        session_id = %session_id,
                        latency_ms = wake_started_at.elapsed().as_millis() as u64,
                        gateway_url = %self.gateway_url,
                        attempt = attempt,
                        next_delay_ms = delay_ms,
                        error = %e,
                    );

                    // Respect deadline while sleeping.
                    let remaining = connect_deadline.saturating_duration_since(Instant::now());
                    let actual_delay = remaining.min(Duration::from_millis(delay_ms));
                    if actual_delay.is_zero() {
                        bail!(
                            "gateway connect deadline exceeded while backing off (attempt {})",
                            attempt
                        );
                    }
                    tokio::time::sleep(actual_delay).await;
                    attempt += 1;
                }
            }
        };

        // ---- Send hello ----
        let hello = ClientMessage::Hello {
            session_id: session_id.to_string(),
            sample_rate,
            encoding: "pcm_s16le",
            channels: 1,
            client: "heyma",
            version: "0.1.0",
        };
        let hello_json = serde_json::to_string(&hello).context("serialize hello")?;
        ws.send(Message::Text(hello_json.into()))
            .await
            .context("send hello")?;

        // ---- F2: Await ready with 10 s timeout ----
        let ready_msg = tokio::time::timeout(
            Duration::from_secs(10),
            receive_json(&mut ws),
        )
        .await
        .map_err(|_| {
            tracing::warn!(
                event = "gateway_phase_timeout",
                phase = "await_ready",
                session_id = %session_id,
                latency_ms = wake_started_at.elapsed().as_millis() as u64,
                gateway_url = %self.gateway_url,
            );
            anyhow::anyhow!("timeout waiting for ready from gateway")
        })??;

        // F13: verify session_id in ready matches what we sent.
        match ready_msg {
            ServerMessage::Ready { session_id: server_sid } => {
                if server_sid != session_id {
                    bail!(
                        "session_id mismatch: sent {}, gateway returned {}",
                        session_id,
                        server_sid
                    );
                }
            }
            ServerMessage::Error { code, message } => {
                bail!("gateway error after hello: [{code}] {message}");
            }
            other => bail!("expected ready, got {:?}", other),
        }

        // ---- F6: Stream PCM frames with concurrent server-message watch ----
        let mut frame_count = 0usize;
        loop {
            tokio::select! {
                biased;
                // Watch for unexpected server messages while we are still streaming.
                server_msg = ws.next() => {
                    match server_msg {
                        None => bail!("gateway closed connection during PCM streaming"),
                        Some(Err(e)) => return Err(e).context("ws recv during PCM streaming"),
                        Some(Ok(Message::Close(_))) => {
                            bail!("gateway closed mid-stream");
                        }
                        Some(Ok(Message::Text(t))) => {
                            let srv: ServerMessage =
                                serde_json::from_str(&t).context("deserialize server msg during streaming")?;
                            match srv {
                                ServerMessage::Error { code, message } => {
                                    bail!("gateway error during PCM stream: [{code}] {message}");
                                }
                                other => bail!("unexpected server message during PCM stream: {:?}", other),
                            }
                        }
                        Some(Ok(Message::Binary(_))) => {
                            // F14: binary before response_start is a protocol error.
                            bail!("unexpected binary frame from gateway during PCM streaming");
                        }
                        Some(Ok(_)) => {
                            // ping/pong handled by tungstenite internally; ignore.
                        }
                    }
                }
                // Receive next audio frame to send.
                frame = audio_rx.recv() => {
                    match frame {
                        None => break, // channel closed = end of utterance
                        Some(f) => {
                            let binary = Message::Binary(f.0.into());
                            ws.send(binary).await.context("send PCM frame")?;
                            frame_count += 1;
                        }
                    }
                }
            }
        }

        // ---- Send end_of_input ----
        let eoi = serde_json::to_string(&ClientMessage::EndOfInput)
            .context("serialize end_of_input")?;
        ws.send(Message::Text(eoi.into()))
            .await
            .context("send end_of_input")?;

        // ---- F2: Await response_start with configurable timeout ----
        let rsp_start_msg = tokio::time::timeout(
            Duration::from_millis(self.response_timeout_ms),
            receive_json_no_binary(&mut ws),
        )
        .await
        .map_err(|_| {
            tracing::warn!(
                event = "gateway_phase_timeout",
                phase = "await_response_start",
                session_id = %session_id,
                latency_ms = wake_started_at.elapsed().as_millis() as u64,
                gateway_url = %self.gateway_url,
            );
            anyhow::anyhow!("timeout waiting for response_start from gateway")
        })??;

        match rsp_start_msg {
            ServerMessage::ResponseStart { .. } => {}
            ServerMessage::Error { code, message } => {
                bail!("gateway error after end_of_input: [{code}] {message}");
            }
            other => bail!("expected response_start, got {:?}", other),
        }

        // ---- Collect WAV binary frames until response_end (F12: cap at 10 MB) ----
        let mut wav_buf: Vec<u8> = Vec::new();
        loop {
            let msg = ws
                .next()
                .await
                .context("ws stream ended before response_end")?
                .context("ws recv")?;
            match msg {
                Message::Binary(b) => {
                    wav_buf.extend_from_slice(&b);
                    // F12: cap at MAX_WAV_BYTES.
                    if wav_buf.len() > MAX_WAV_BYTES {
                        bail!(
                            "gateway response too large: {} bytes exceeds {} byte cap",
                            wav_buf.len(),
                            MAX_WAV_BYTES
                        );
                    }
                }
                Message::Text(t) => {
                    let srv: ServerMessage =
                        serde_json::from_str(&t).context("deserialize server msg")?;
                    match srv {
                        ServerMessage::ResponseEnd => break,
                        ServerMessage::Error { code, message } => {
                            bail!("gateway error during WAV recv: [{code}] {message}");
                        }
                        other => bail!("unexpected message during WAV recv: {:?}", other),
                    }
                }
                Message::Close(_) => bail!("gateway closed mid-response"),
                _ => {} // ping/pong handled by tungstenite internally
            }
        }

        // ---- Send close ----
        let close_msg =
            serde_json::to_string(&ClientMessage::Close).context("serialize close")?;
        // Best-effort; ignore error (gateway may have already closed).
        let _ = ws.send(Message::Text(close_msg.into())).await;

        // ---- Play WAV ----
        sink.play_wav(Bytes::from(wav_buf))
            .context("play WAV response")?;

        Ok(frame_count)
    }
}

// ---------------------------------------------------------------------------
// Helper: receive next text message and deserialize as ServerMessage.
// Silently skips ping/pong; errors on binary frames.
// ---------------------------------------------------------------------------

async fn receive_json(
    ws: &mut WebSocketStream<MaybeTlsStream<TcpStream>>,
) -> Result<ServerMessage> {
    loop {
        let msg = ws
            .next()
            .await
            .context("ws stream closed unexpectedly")?
            .context("ws recv")?;
        match msg {
            Message::Text(t) => {
                let srv: ServerMessage =
                    serde_json::from_str(&t).context("deserialize server message")?;
                return Ok(srv);
            }
            Message::Close(_) => bail!("gateway closed connection"),
            _ => continue, // skip ping/pong/binary here
        }
    }
}

// ---------------------------------------------------------------------------
// Helper: like receive_json but treats binary frames as protocol errors (F14).
// Used while awaiting response_start after end_of_input.
// ---------------------------------------------------------------------------

async fn receive_json_no_binary(
    ws: &mut WebSocketStream<MaybeTlsStream<TcpStream>>,
) -> Result<ServerMessage> {
    loop {
        let msg = ws
            .next()
            .await
            .context("ws stream closed unexpectedly")?
            .context("ws recv")?;
        match msg {
            Message::Text(t) => {
                let srv: ServerMessage =
                    serde_json::from_str(&t).context("deserialize server message")?;
                return Ok(srv);
            }
            Message::Close(_) => bail!("gateway closed connection"),
            // F14: binary before response_start is a protocol error.
            Message::Binary(_) => bail!("unexpected binary frame while awaiting response_start"),
            _ => continue, // skip ping/pong
        }
    }
}
