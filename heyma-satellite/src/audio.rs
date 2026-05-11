use crate::config::Settings;
use anyhow::{Context, Result};
use bytes::Bytes;
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use hound;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use tokio::sync::mpsc;

// ---------------------------------------------------------------------------
// Shared frame type
// ---------------------------------------------------------------------------

/// A single audio frame: raw S16_LE PCM bytes, 80 ms at 16 kHz mono = 2560 bytes.
#[derive(Debug, Clone)]
pub struct AudioFrame(pub Bytes);

impl AudioFrame {
    /// Construct from a vec of i16 samples (native endian -> little-endian bytes).
    pub fn from_samples(samples: &[i16]) -> Self {
        let mut buf = Vec::with_capacity(samples.len() * 2);
        for s in samples {
            buf.extend_from_slice(&s.to_le_bytes());
        }
        AudioFrame(Bytes::from(buf))
    }

    /// Number of bytes in the frame.
    pub fn len(&self) -> usize {
        self.0.len()
    }

    /// True when the frame carries zero bytes.
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    /// View frame bytes as a slice of i16 samples (interpreting as little-endian).
    pub fn as_samples(&self) -> Vec<i16> {
        self.0
            .chunks_exact(2)
            .map(|b| i16::from_le_bytes([b[0], b[1]]))
            .collect()
    }

    /// D2 watchdog support: sum-of-squares and peak |sample| in one pass, no allocation.
    /// Caller accumulates `sumsq` across frames to compute window RMS:
    ///   rms = sqrt(total_sumsq / total_sample_count)
    /// `peak` is u32 so i16::MIN (which overflows abs()) is representable as 32768.
    pub fn sumsq_and_peak(&self) -> (f64, u32) {
        let mut sumsq = 0.0f64;
        let mut peak: u32 = 0;
        for chunk in self.0.chunks_exact(2) {
            let s = i16::from_le_bytes([chunk[0], chunk[1]]);
            let sf = s as f64;
            sumsq += sf * sf;
            let abs_s = (s as i32).unsigned_abs();
            if abs_s > peak {
                peak = abs_s;
            }
        }
        (sumsq, peak)
    }
}

// ---------------------------------------------------------------------------
// AudioSource trait
// ---------------------------------------------------------------------------

/// Trait seam for audio capture. Concrete impl is `CpalAudioSource`; tests use stubs.
pub trait AudioSource: Send + 'static {
    /// Start producing frames. Returns the receiver end of an mpsc channel.
    /// The source runs in the background and pushes `AudioFrame` values until
    /// the returned receiver is dropped or the underlying stream ends.
    fn start(self: Box<Self>) -> Result<mpsc::Receiver<AudioFrame>>;
}

// ---------------------------------------------------------------------------
// AudioSink trait
// ---------------------------------------------------------------------------

/// Trait seam for audio playback. Concrete impl is `CpalAudioSink`; tests use stubs.
pub trait AudioSink: Send + 'static {
    /// Play a complete WAV byte buffer synchronously (blocking until playback ends).
    fn play_wav(&mut self, wav_bytes: Bytes) -> Result<()>;
}

// ---------------------------------------------------------------------------
// CpalAudioSource -- production microphone capture
// ---------------------------------------------------------------------------

pub struct CpalAudioSource {
    settings: Arc<Settings>,
}

impl CpalAudioSource {
    pub fn new(settings: Arc<Settings>) -> Self {
        CpalAudioSource { settings }
    }
}

impl AudioSource for CpalAudioSource {
    fn start(self: Box<Self>) -> Result<mpsc::Receiver<AudioFrame>> {
        let (tx, rx) = mpsc::channel(64);
        let settings = self.settings.clone();
        let sample_rate = settings.sample_rate;
        // 80 ms frame = sample_rate * 0.080 samples; mono S16_LE.
        let frame_samples = (sample_rate as f64 * 0.080) as usize;

        std::thread::spawn(move || -> Result<()> {
            let host = cpal::default_host();

            let device = match &settings.mic_device {
                Some(name) => host
                    .input_devices()
                    .context("enumerate input devices")?
                    .find(|d| d.name().map(|n| n.contains(name.as_str())).unwrap_or(false))
                    .with_context(|| format!("mic device not found: {name}"))?,
                None => host
                    .default_input_device()
                    .context("no default input device")?,
            };

            let config = cpal::StreamConfig {
                channels: 1,
                sample_rate: cpal::SampleRate(sample_rate),
                buffer_size: cpal::BufferSize::Default,
            };

            // F4: atomic flag so err_fn can signal the park loop to exit.
            let died = Arc::new(AtomicBool::new(false));
            let died_for_err = died.clone();

            let mut accumulator: Vec<i16> = Vec::with_capacity(frame_samples * 2);
            let tx_clone = tx.clone();

            let stream = device
                .build_input_stream(
                    &config,
                    move |data: &[i16], _| {
                        accumulator.extend_from_slice(data);
                        while accumulator.len() >= frame_samples {
                            let frame_data: Vec<i16> =
                                accumulator.drain(..frame_samples).collect();
                            let frame = AudioFrame::from_samples(&frame_data);
                            // F4: use try_send (realtime-safe); dropped frames are
                            // counted and logged by the supervisor (F3).
                            if tx_clone.try_send(frame).is_err() {
                                // Receiver dropped; stop producing.
                                return;
                            }
                        }
                    },
                    move |err| {
                        tracing::error!(event = "audio_input_error", error = %err);
                        // F4: signal the park loop to exit so the stream is dropped.
                        died_for_err.store(true, Ordering::Relaxed);
                    },
                    None,
                )
                .context("build_input_stream")?;

            stream.play().context("start input stream")?;

            // F4: park with 1-second timeout so the died flag is checked periodically.
            // When the flag fires (USB unplug / stream error), break -> drop stream + tx.
            loop {
                std::thread::park_timeout(std::time::Duration::from_secs(1));
                if died.load(Ordering::Relaxed) {
                    tracing::warn!(event = "mic_stream_died");
                    break;
                }
            }
            // Dropping `stream` and `tx_clone` here causes mic_rx.recv() -> None in
            // the supervisor, which logs mic_source_ended and exits.
            drop(stream);
            Ok(())
        });

        Ok(rx)
    }
}

// ---------------------------------------------------------------------------
// CpalAudioSink -- production speaker playback
// ---------------------------------------------------------------------------

pub struct CpalAudioSink {
    settings: Arc<Settings>,
}

impl CpalAudioSink {
    pub fn new(settings: Arc<Settings>) -> Self {
        CpalAudioSink { settings }
    }
}

impl AudioSink for CpalAudioSink {
    fn play_wav(&mut self, wav_bytes: Bytes) -> Result<()> {
        use std::io::Cursor;

        let host = cpal::default_host();

        let device = match &self.settings.speaker_device {
            Some(name) => host
                .output_devices()
                .context("enumerate output devices")?
                .find(|d| d.name().map(|n| n.contains(name.as_str())).unwrap_or(false))
                .with_context(|| format!("speaker device not found: {name}"))?,
            None => host
                .default_output_device()
                .context("no default output device")?,
        };

        // Parse WAV header to extract playback parameters.
        let cursor = Cursor::new(wav_bytes.as_ref());
        let mut reader = hound::WavReader::new(cursor).context("parse WAV")?;
        let spec = reader.spec();
        let samples: Vec<i16> = reader
            .samples::<i16>()
            .collect::<std::result::Result<_, _>>()
            .context("decode WAV samples")?;

        let config = cpal::StreamConfig {
            channels: spec.channels,
            sample_rate: cpal::SampleRate(spec.sample_rate),
            buffer_size: cpal::BufferSize::Default,
        };

        let samples = Arc::new(samples);
        let position = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let done = Arc::new(AtomicBool::new(false));

        let samples_clone = samples.clone();
        let position_clone = position.clone();
        let done_clone = done.clone();

        let stream = device
            .build_output_stream(
                &config,
                move |output: &mut [i16], _| {
                    let pos = position_clone.load(Ordering::Relaxed);
                    let remaining = &samples_clone[pos..];
                    let to_write = output.len().min(remaining.len());
                    output[..to_write].copy_from_slice(&remaining[..to_write]);
                    // Zero-fill tail if we ran out of samples.
                    for o in output[to_write..].iter_mut() {
                        *o = 0;
                    }
                    position_clone.fetch_add(to_write, Ordering::Relaxed);
                    if pos + to_write >= samples_clone.len() {
                        done_clone.store(true, Ordering::Relaxed);
                    }
                },
                |err| {
                    tracing::error!(event = "audio_output_error", error = %err);
                },
                None,
            )
            .context("build_output_stream")?;

        stream.play().context("start output stream")?;

        // F5: spin-wait with a 30 s ceiling to prevent infinite blocking.
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
        loop {
            if done.load(Ordering::Relaxed) {
                break;
            }
            if std::time::Instant::now() >= deadline {
                tracing::warn!(event = "playback_timeout");
                drop(stream);
                return Err(anyhow::anyhow!("playback timed out after 30 seconds"));
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        // Drop stream explicitly to release the device.
        drop(stream);
        Ok(())
    }
}
