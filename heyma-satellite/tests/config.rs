// tests/config.rs — Settings hydration via HEYMA_* env vars.

// Pull in the library modules via the binary crate.
// Since this is a binary-only crate, we use a path-based approach.
// We duplicate the minimal config module types here via the public API
// exposed from the compiled binary's source. For integration tests of a
// binary crate, we use `#[path]` includes.

#[path = "../src/config.rs"]
mod config;

use config::{ConfigError, Settings};
use std::sync::Mutex;

// Env-var tests must be serialized to avoid races between parallel test threads.
static ENV_MUTEX: Mutex<()> = Mutex::new(());

/// Helper: acquire the env mutex and run a closure with a set of env vars set,
/// then unset them before releasing the lock.
fn with_env<F, R>(pairs: &[(&str, &str)], f: F) -> R
where
    F: FnOnce() -> R,
{
    let _guard = ENV_MUTEX.lock().unwrap_or_else(|e| e.into_inner());
    for (k, v) in pairs {
        // SAFETY: env vars are global mutable state; the mutex ensures only one
        // test modifies them at a time.
        #[allow(unsafe_code)]
        unsafe {
            std::env::set_var(k, v);
        }
    }
    let result = f();
    for (k, _) in pairs {
        #[allow(unsafe_code)]
        unsafe {
            std::env::remove_var(k);
        }
    }
    result
}

#[test]
fn test_defaults_are_valid() {
    let settings = Settings::default();
    settings.validate().expect("defaults must be valid");
}

#[test]
fn test_default_gateway_url() {
    let settings = Settings::default();
    assert_eq!(settings.gateway_url, "ws://192.168.1.12:8778/v1/voice");
}

#[test]
fn test_default_sample_rate() {
    let settings = Settings::default();
    assert_eq!(settings.sample_rate, 16_000);
}

#[test]
fn test_default_wake_threshold() {
    let settings = Settings::default();
    assert!((settings.wake_threshold - 0.5).abs() < f32::EPSILON);
}

#[test]
fn test_default_silence_threshold_db() {
    let settings = Settings::default();
    assert!((settings.silence_threshold_db - (-40.0)).abs() < f32::EPSILON);
}

#[test]
fn test_default_min_max_utterance_ms() {
    let settings = Settings::default();
    assert_eq!(settings.min_utterance_ms, 500);
    assert_eq!(settings.max_utterance_ms, 30_000);
}

#[test]
fn test_env_var_overrides_gateway_url() {
    with_env(
        &[("HEYMA_GATEWAY_URL", "ws://10.0.0.1:9999/v1/voice")],
        || {
            let settings = Settings::from_env().expect("load settings");
            assert_eq!(settings.gateway_url, "ws://10.0.0.1:9999/v1/voice");
        },
    );
}

#[test]
fn test_env_var_rejects_non_16000_sample_rate() {
    // F11: sample_rate must be exactly 16000; any other value is rejected.
    with_env(&[("HEYMA_SAMPLE_RATE", "44100")], || {
        let err = Settings::from_env().expect_err("44100 Hz must be rejected");
        assert!(matches!(err, ConfigError::UnsupportedSampleRate(44_100)));
    });
}

#[test]
fn test_env_var_overrides_wake_threshold() {
    with_env(&[("HEYMA_WAKE_THRESHOLD", "0.75")], || {
        let settings = Settings::from_env().expect("load settings");
        assert!((settings.wake_threshold - 0.75).abs() < 1e-5);
    });
}

#[test]
fn test_env_var_sets_mic_device() {
    with_env(&[("HEYMA_MIC_DEVICE", "ReSpeaker")], || {
        let settings = Settings::from_env().expect("load settings");
        assert_eq!(settings.mic_device, Some("ReSpeaker".to_string()));
    });
}

#[test]
fn test_env_var_sets_speaker_device() {
    with_env(&[("HEYMA_SPEAKER_DEVICE", "plughw:0,0")], || {
        let settings = Settings::from_env().expect("load settings");
        assert_eq!(settings.speaker_device, Some("plughw:0,0".to_string()));
    });
}

#[test]
fn test_env_var_sets_min_max_utterance_ms() {
    with_env(
        &[
            ("HEYMA_MIN_UTTERANCE_MS", "750"),
            ("HEYMA_MAX_UTTERANCE_MS", "20000"),
        ],
        || {
            let settings = Settings::from_env().expect("load settings");
            assert_eq!(settings.min_utterance_ms, 750);
            assert_eq!(settings.max_utterance_ms, 20_000);
        },
    );
}

#[test]
fn test_validation_rejects_wake_threshold_above_one() {
    let mut settings = Settings::default();
    settings.wake_threshold = 1.5;
    let err = settings.validate().expect_err("must reject threshold > 1.0");
    assert!(matches!(err, ConfigError::InvalidWakeThreshold(_)));
}

#[test]
fn test_validation_rejects_wake_threshold_below_zero() {
    let mut settings = Settings::default();
    settings.wake_threshold = -0.1;
    let err = settings.validate().expect_err("must reject threshold < 0.0");
    assert!(matches!(err, ConfigError::InvalidWakeThreshold(_)));
}

#[test]
fn test_validation_rejects_non_16000_sample_rate() {
    let mut settings = Settings::default();
    settings.sample_rate = 44_100;
    let err = settings.validate().expect_err("must reject sample_rate != 16000");
    assert!(matches!(err, ConfigError::UnsupportedSampleRate(44_100)));
}

#[test]
fn test_validation_rejects_zero_sample_rate() {
    let mut settings = Settings::default();
    settings.sample_rate = 0;
    let err = settings.validate().expect_err("must reject sample_rate = 0");
    assert!(matches!(err, ConfigError::UnsupportedSampleRate(0)));
}

#[test]
fn test_validation_rejects_invalid_gateway_url_scheme() {
    let mut settings = Settings::default();
    settings.gateway_url = "http://example.com/v1/voice".to_string();
    let err = settings
        .validate()
        .expect_err("must reject non-ws gateway URL");
    assert!(matches!(err, ConfigError::InvalidGatewayUrlScheme(_)));
}

#[test]
fn test_validation_accepts_wss_gateway_url() {
    let mut settings = Settings::default();
    settings.gateway_url = "wss://example.com/v1/voice".to_string();
    settings.validate().expect("wss:// must be accepted");
}

#[test]
fn test_validation_accepts_ws_gateway_url() {
    let settings = Settings::default();
    // Default is ws:// so this validates the default passes.
    settings.validate().expect("default ws:// URL must be accepted");
}

#[test]
fn test_validation_rejects_min_gte_max_utterance() {
    let mut settings = Settings::default();
    settings.min_utterance_ms = 5_000;
    settings.max_utterance_ms = 5_000;
    let err = settings
        .validate()
        .expect_err("must reject min_utterance_ms >= max_utterance_ms");
    assert!(matches!(
        err,
        ConfigError::InvalidUtteranceBounds { .. }
    ));
}

#[test]
fn test_round_trip_all_fields() {
    with_env(
        &[
            ("HEYMA_GATEWAY_URL", "ws://127.0.0.1:8778/v1/voice"),
            (
                "HEYMA_WAKE_MODEL_PATH",
                "/tmp/hey_tonny.onnx",
            ),
            ("HEYMA_WAKE_THRESHOLD", "0.6"),
            ("HEYMA_MIC_DEVICE", "hw:1,0"),
            ("HEYMA_SPEAKER_DEVICE", "hw:1,1"),
            ("HEYMA_SAMPLE_RATE", "16000"),
            ("HEYMA_SILENCE_THRESHOLD_DB", "-35.0"),
            ("HEYMA_MIN_UTTERANCE_MS", "400"),
            ("HEYMA_MAX_UTTERANCE_MS", "25000"),
        ],
        || {
            let s = Settings::from_env().expect("load settings");
            assert_eq!(s.gateway_url, "ws://127.0.0.1:8778/v1/voice");
            assert_eq!(s.wake_model_path.to_str().unwrap(), "/tmp/hey_tonny.onnx");
            assert!((s.wake_threshold - 0.6).abs() < 1e-5);
            assert_eq!(s.mic_device, Some("hw:1,0".to_string()));
            assert_eq!(s.speaker_device, Some("hw:1,1".to_string()));
            assert_eq!(s.sample_rate, 16_000);
            assert!((s.silence_threshold_db - (-35.0)).abs() < 1e-5);
            assert_eq!(s.min_utterance_ms, 400);
            assert_eq!(s.max_utterance_ms, 25_000);
        },
    );
}
