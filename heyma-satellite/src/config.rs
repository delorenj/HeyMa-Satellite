use figment::{providers::Env, Figment};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use thiserror::Error;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Settings {
    /// WebSocket URL of heyma-gateway endpoint.
    #[serde(default = "default_gateway_url")]
    pub gateway_url: String,

    /// Path to the ONNX wake-word model file (hey_tonny.onnx).
    #[serde(default = "default_wake_model_path")]
    pub wake_model_path: PathBuf,

    /// Minimum confidence threshold for wake detection (0.0–1.0).
    #[serde(default = "default_wake_threshold")]
    pub wake_threshold: f32,

    /// ALSA/cpal device name for microphone capture. `None` = system default.
    #[serde(default)]
    pub mic_device: Option<String>,

    /// ALSA/cpal device name for speaker playback. `None` = system default.
    #[serde(default)]
    pub speaker_device: Option<String>,

    /// PCM sample rate in Hz. Must match the wake model expectation (16 kHz).
    #[serde(default = "default_sample_rate")]
    pub sample_rate: u32,

    /// Silence detection floor in dBFS. Audio below this level is considered silence.
    #[serde(default = "default_silence_threshold_db")]
    pub silence_threshold_db: f32,

    /// Minimum utterance duration in milliseconds before EOU can fire.
    #[serde(default = "default_min_utterance_ms")]
    pub min_utterance_ms: u32,

    /// Maximum utterance duration in milliseconds; EOU fires unconditionally at this ceiling.
    #[serde(default = "default_max_utterance_ms")]
    pub max_utterance_ms: u32,

    /// Timeout for awaiting response_start after end_of_input is sent (milliseconds).
    #[serde(default = "default_gateway_response_timeout_ms")]
    pub gateway_response_timeout_ms: u64,

    /// Overall deadline for the gateway connect-retry loop (milliseconds).
    #[serde(default = "default_gateway_connect_deadline_ms")]
    pub gateway_connect_deadline_ms: u64,
}

fn default_gateway_url() -> String {
    "ws://192.168.1.12:8778/v1/voice".to_string()
}

fn default_wake_model_path() -> PathBuf {
    PathBuf::from("/home/delorenj/custom_wakewords/hey_tonny.onnx")
}

fn default_wake_threshold() -> f32 {
    0.5
}

fn default_sample_rate() -> u32 {
    16_000
}

fn default_silence_threshold_db() -> f32 {
    -40.0
}

fn default_min_utterance_ms() -> u32 {
    500
}

fn default_max_utterance_ms() -> u32 {
    30_000
}

fn default_gateway_response_timeout_ms() -> u64 {
    30_000
}

fn default_gateway_connect_deadline_ms() -> u64 {
    60_000
}

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("figment extraction failed: {0}")]
    Figment(#[from] figment::Error),

    #[error("wake_threshold must be in 0.0..=1.0, got {0}")]
    InvalidWakeThreshold(f32),

    #[error("sample_rate must be 16000, got {0}")]
    UnsupportedSampleRate(u32),

    #[error("gateway_url must begin with ws:// or wss://, got: {0}")]
    InvalidGatewayUrlScheme(String),

    #[error("min_utterance_ms ({min}) must be < max_utterance_ms ({max})")]
    InvalidUtteranceBounds { min: u32, max: u32 },
}

impl Settings {
    /// Load settings from `HEYMA_*` environment variables, with sane defaults.
    pub fn from_env() -> Result<Self, ConfigError> {
        let settings: Settings = Figment::new()
            .merge(Env::prefixed("HEYMA_"))
            .extract()?;
        settings.validate()?;
        Ok(settings)
    }

    /// Validate field invariants.
    pub fn validate(&self) -> Result<(), ConfigError> {
        // F10: gateway URL must use ws:// or wss:// scheme.
        if !self.gateway_url.starts_with("ws://") && !self.gateway_url.starts_with("wss://") {
            return Err(ConfigError::InvalidGatewayUrlScheme(self.gateway_url.clone()));
        }
        if !(0.0..=1.0).contains(&self.wake_threshold) {
            return Err(ConfigError::InvalidWakeThreshold(self.wake_threshold));
        }
        // F11: sample_rate must be exactly 16000 (wake model is trained at 16 kHz).
        if self.sample_rate != 16_000 {
            return Err(ConfigError::UnsupportedSampleRate(self.sample_rate));
        }
        if self.min_utterance_ms >= self.max_utterance_ms {
            return Err(ConfigError::InvalidUtteranceBounds {
                min: self.min_utterance_ms,
                max: self.max_utterance_ms,
            });
        }
        Ok(())
    }
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            gateway_url: default_gateway_url(),
            wake_model_path: default_wake_model_path(),
            wake_threshold: default_wake_threshold(),
            mic_device: None,
            speaker_device: None,
            sample_rate: default_sample_rate(),
            silence_threshold_db: default_silence_threshold_db(),
            min_utterance_ms: default_min_utterance_ms(),
            max_utterance_ms: default_max_utterance_ms(),
            gateway_response_timeout_ms: default_gateway_response_timeout_ms(),
            gateway_connect_deadline_ms: default_gateway_connect_deadline_ms(),
        }
    }
}
