import { MAX_WAV_BYTES, captureMicrophone, checkAbort, pcmToWav, validateWav } from "./audio.js";
import { submitTurn } from "./protocol.js";

const byId = (id) => document.getElementById(id);
const ui = Object.fromEntries([
  "record", "stop-send", "cancel", "duration", "wav-upload", "capture-progress", "capture-time",
  "mode", "mode-description", "privacy-note", "status-title", "status-detail", "error",
  "reply-player", "reply-audio", "reply-details", "play-reply", "download-request", "download-reply",
  "gateway-state", "evidence", "reset",
].map((id) => [id, byId(id)]));

let turn = null;
let stopRecording = null;
let resetting = false;
let requestURL = null;
let replyURL = null;

function controls() {
  const busy = Boolean(turn) || resetting;
  const recording = Boolean(turn && stopRecording);
  ui.record.disabled = busy;
  ui.record.hidden = recording;
  ui["stop-send"].hidden = !recording;
  ui["stop-send"].disabled = !recording;
  ui.cancel.hidden = !turn;
  ui.duration.disabled = busy;
  ui["wav-upload"].disabled = busy;
  ui.reset.disabled = busy;
  ui["play-reply"].disabled = busy;
}

function status(phase, title, detail) {
  document.body.dataset.phase = phase;
  ui["status-title"].textContent = title;
  ui["status-detail"].textContent = detail;
}

function clearError() {
  ui.error.hidden = true;
  ui.error.textContent = "";
  delete ui.error.dataset.code;
}

function showError(code, message) {
  ui.error.textContent = message;
  ui.error.dataset.code = code;
  ui.error.hidden = false;
}

function errorMessage(error) {
  const code = typeof error.code === "string" ? error.code : error.name;
  const messages = {
    NotAllowedError: "Microphone access was denied. Allow microphone access in your browser's site settings, then record again. WAV uploads also work.",
    NotFoundError: "No microphone was found. Connect one or upload a WAV.",
    NotReadableError: "The microphone could not be opened. Check whether another application is using it, or upload a WAV.",
    microphone_unavailable: "Microphone capture needs a current browser on localhost or HTTPS. You can still upload a WAV.",
    microphone_permission_timeout: "The microphone prompt timed out. Choose Allow in your browser, then press Record again, or upload a WAV.",
    microphone_disconnected: "The microphone disconnected. Reconnect it and record again.",
    capture_timeout: "The microphone stopped delivering audio. Keep this tab active and record again.",
    capture_failed: "Recording failed. Check your microphone and record again, or upload a WAV.",
    empty_capture: "The recording was empty. Record for a little longer before sending.",
    capture_too_large: "Audio must be no longer than 15 seconds. Choose a shorter recording or WAV.",
    wav_too_large: "The WAV file exceeds the 4 MiB limit. Use a 16 kHz mono PCM16 WAV of up to 15 seconds.",
    input_wav_must_be_16000hz_mono_pcm16: "Upload a 16 kHz mono PCM16 WAV. Other formats must be converted before uploading.",
    busy: "Tonny is handling another turn. Wait for it to finish, then record or upload again.",
    no_speech: "No speech was recognized. Try speaking closer to the microphone or upload a speech WAV.",
    not_configured: "Live voice providers are not configured. Start the local stack with live credentials, or use offline mode.",
    connection_unavailable: "The gateway could not be reached within 60 seconds. Start or check the local stack, then submit a new turn.",
    connection_lost: "The connection ended before a complete reply arrived. This turn was not resubmitted. Check the gateway, then try a new turn.",
    response_timeout: "The reply took too long. The connection was cancelled. Check the gateway and submit a new turn.",
    timeout: "The gateway timed out. Try a shorter message or check its provider connection.",
    provider_error: "A live voice provider could not complete the turn. Check the gateway logs and provider connection, then try again.",
    invalid_response: "The gateway returned an invalid or incomplete audio reply. Check its logs, then try a new turn.",
    response_too_large: "The reply exceeded the audio size limit. Try a shorter request.",
    reset_busy: "A turn is still active. Wait for it to finish before resetting the conversation.",
    reset_failed: "The conversation could not be reset. Check that the local gateway is running and try again.",
  };
  if (messages[code]) return { code, message: messages[code] };
  if (typeof code === "string" && (code.includes("wav") || code === "invalid_pcm_samples")) {
    return { code, message: "The WAV is malformed or incomplete. Use a valid 16 kHz mono PCM16 WAV of up to 15 seconds." };
  }
  return { code: "turn_failed", message: "Tonny could not complete this turn. Check the gateway logs, then record or upload again." };
}

function report(error) {
  if (error.name === "AbortError") return;
  const { code, message } = errorMessage(error);
  status("error", "Turn could not finish", requestURL
    ? "Your submitted WAV is available below the recording controls."
    : "Record again or choose another WAV when you’re ready.");
  showError(code, message);
}

function timing(seconds = 0) {
  const limit = Number(ui.duration.value);
  ui["capture-progress"].max = limit;
  ui["capture-progress"].value = seconds;
  ui["capture-time"].textContent = `${seconds.toFixed(1)} / ${limit.toFixed(1)} s`;
}

function clearArtifacts() {
  ui["reply-audio"].pause();
  ui["reply-audio"].removeAttribute("src");
  ui["reply-audio"].load();
  ui["reply-player"].hidden = true;
  ui["download-request"].hidden = true;
  ui["download-request"].removeAttribute("href");
  ui["download-reply"].removeAttribute("href");
  if (requestURL) URL.revokeObjectURL(requestURL);
  if (replyURL) URL.revokeObjectURL(replyURL);
  requestURL = replyURL = null;
}

function startTurn() {
  if (turn || resetting) return null;
  clearArtifacts();
  clearError();
  timing();
  turn = { controller: new AbortController() };
  stopRecording = null;
  controls();
  return turn;
}

function isCurrent(current) { return turn === current && !current.controller.signal.aborted; }

function finishTurn(current) {
  if (turn !== current) return;
  turn = null;
  stopRecording = null;
  controls();
  void refreshHealth();
}

function playReply() {
  if (!replyURL) return;
  const playingURL = replyURL;
  clearError();
  void ui["reply-audio"].play().catch(() => {
    if (replyURL !== playingURL) return;
    status("ready", "Reply ready", "Press Play reply or use the audio player to listen.");
    showError("playback_blocked", "Automatic playback was unavailable. Press Play reply, use the audio controls, or download the WAV.");
  });
}

async function sendPCM(current, pcm) {
  checkAbort(current.controller.signal);
  const wav = pcmToWav(pcm);
  requestURL = URL.createObjectURL(new Blob([wav], { type: "audio/wav" }));
  ui["download-request"].href = requestURL;
  ui["download-request"].hidden = false;
  stopRecording = null;
  controls();
  const reply = await submitTurn(pcm, {
    signal: current.controller.signal,
    onState: (phase, attempt) => {
      if (!isCurrent(current)) return;
      if (phase === "processing") {
        status("processing", "Waiting for Tonny", "Audio sent. Preparing the reply…");
      } else if (phase === "reconnecting") {
        status("connecting", "Reconnecting to the gateway", "Keeping your recording in memory while the local stack reconnects. You can cancel at any time.");
      } else {
        status("connecting", "Connecting to the gateway", attempt > 1 ? `Connection attempt ${attempt}.` : "Your recording is ready to send.");
      }
    },
  });
  if (!isCurrent(current)) return;
  const info = validateWav(reply);
  replyURL = URL.createObjectURL(new Blob([reply], { type: "audio/wav" }));
  ui["reply-audio"].src = replyURL;
  ui["download-reply"].href = replyURL;
  ui["reply-details"].textContent = `${info.seconds.toFixed(2)} seconds · ${(info.sampleRate / 1000).toFixed(0)} kHz · ${info.channels === 1 ? "mono" : "stereo"} · PCM${info.bits}`;
  ui["reply-player"].hidden = false;
  status("ready", "Reply ready", "The full audio reply arrived and passed validation.");
  playReply();
}

async function record() {
  const current = startTurn();
  if (!current) return;
  status("requesting", "Opening the microphone", "Allow microphone access if your browser asks. Cancel is available while you wait.");
  try {
    const pcm = await captureMicrophone({
      seconds: Number(ui.duration.value), signal: current.controller.signal,
      onReady: (stop) => {
        if (!isCurrent(current)) return;
        stopRecording = stop;
        status("recording", "Listening", "Speak now. Stop and send when you’re done, or wait for the recording limit.");
        controls();
      },
      onProgress: (seconds) => { if (isCurrent(current)) timing(seconds); },
    });
    if (isCurrent(current)) await sendPCM(current, pcm);
  } catch (error) {
    if (isCurrent(current)) report(error);
  } finally {
    finishTurn(current);
  }
}

async function upload(file) {
  const current = startTurn();
  if (!current) return;
  status("processing", "Checking the WAV", "Validating the format and duration before sending.");
  try {
    if (file.size > MAX_WAV_BYTES) throw Object.assign(new Error(), { code: "wav_too_large" });
    const wav = new Uint8Array(await file.arrayBuffer());
    checkAbort(current.controller.signal);
    const { pcm } = validateWav(wav, { capture: true });
    if (isCurrent(current)) await sendPCM(current, pcm);
  } catch (error) {
    if (isCurrent(current)) report(error);
  } finally {
    finishTurn(current);
  }
}

function cancel() {
  const current = turn;
  turn = null;
  stopRecording = null;
  current?.controller.abort();
  ui["reply-audio"].pause();
  clearError();
  status("idle", "Cancelled", "Ready for a new recording or WAV.");
  controls();
  void refreshHealth();
}

async function fetchLocal(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try { return await fetch(path, { ...options, cache: "no-store", signal: controller.signal }); }
  finally { clearTimeout(timer); }
}

async function refreshHealth() {
  try {
    const response = await fetchLocal("/healthz");
    if (!response.ok) throw new Error("Health request failed");
    const health = await response.json();
    const mode = health.mode;
    ui.mode.dataset.mode = mode === "loopback" || mode === "live" ? mode : "unknown";
    if (mode === "loopback") {
      ui.mode.textContent = "Offline echo";
      ui["mode-description"].textContent = "Your recording comes straight back through Tonny. No speech recognition or AI reply in this mode.";
      ui["privacy-note"].textContent = "Offline echo keeps audio in your local stack. No voice provider calls.";
    } else if (mode === "live") {
      ui.mode.textContent = "Live voice";
      ui["mode-description"].textContent = "Tonny listens, thinks, and speaks using the live voice providers.";
      ui["privacy-note"].textContent = "Live mode sends speech to Deepgram, text to OpenRouter, and reply text to Cartesia.";
    } else {
      ui.mode.textContent = "Mode unavailable";
      ui["mode-description"].textContent = "This gateway did not report its mode. Check that the local development stack is up to date.";
      ui["privacy-note"].textContent = "Check the gateway configuration before submitting audio.";
    }
    const missing = Object.entries(health.configured || {}).filter(([, configured]) => !configured).map(([name]) => name);
    ui["gateway-state"].textContent = mode === "live" && missing.length ? "Credentials needed" : health.active_session ? "Turn active" : "Connected";
    const evidence = health.evidence_since_start || {};
    const integer = (value) => Number.isInteger(value) && value >= 0 ? value : 0;
    ui.evidence.textContent = `${integer(evidence.sessions)} sessions · ${integer(evidence.responses_sent)} replies since gateway start. `
      + (mode === "live" && missing.length ? `Missing: ${missing.join(", ")}.` : `Provider turns: ${integer(evidence.stt_turns)} STT / ${integer(evidence.llm_turns)} LLM / ${integer(evidence.tts_turns)} TTS.`);
  } catch {
    ui["gateway-state"].textContent = "Unavailable";
    ui.evidence.textContent = "Check that the local stack is running. Connection status refreshes automatically.";
  }
}

async function reset() {
  if (turn || resetting) return;
  resetting = true;
  controls();
  clearError();
  try {
    const response = await fetchLocal("/v1/reset", { method: "POST" });
    if (!response.ok) throw Object.assign(new Error(), { code: response.status === 409 ? "reset_busy" : "reset_failed" });
    ui["reply-audio"].pause();
    status("idle", "Conversation reset", "The gateway is ready for a fresh conversation.");
  } catch (error) {
    const { code, message } = errorMessage(typeof error.code === "string" ? error : { code: "reset_failed" });
    showError(code, message);
  } finally {
    resetting = false;
    controls();
    void refreshHealth();
  }
}

ui.record.addEventListener("click", () => void record());
ui["stop-send"].addEventListener("click", () => {
  if (!stopRecording) return;
  ui["stop-send"].disabled = true;
  status("processing", "Finishing the recording", "Releasing the microphone and preparing your audio.");
  stopRecording();
});
ui.cancel.addEventListener("click", cancel);
ui.duration.addEventListener("change", () => timing());
ui["wav-upload"].addEventListener("change", () => {
  const [file] = ui["wav-upload"].files;
  ui["wav-upload"].value = ""; // Selecting the same fixture again must trigger another turn.
  if (file) void upload(file);
});
ui["play-reply"].addEventListener("click", playReply);
ui.reset.addEventListener("click", () => void reset());
ui["reply-audio"].addEventListener("playing", () => {
  if (replyURL) status("playback", "Playing Tonny’s reply", "You can pause, replay, or download the audio below.");
});
ui["reply-audio"].addEventListener("ended", () => {
  if (replyURL) status("ready", "Reply ready", "Record another message, replay this reply, or download the WAV.");
});
ui["reply-audio"].addEventListener("error", () => {
  if (replyURL) showError("playback_failed", "This browser could not play the reply. Download the validated WAV to listen in another player.");
});

timing();
controls();
void refreshHealth();
let healthTimer = setInterval(() => void refreshHealth(), 10000);
window.addEventListener("pagehide", () => {
  clearInterval(healthTimer);
  turn?.controller.abort();
  clearArtifacts();
});
window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;
  turn = null;
  stopRecording = null;
  controls();
  status("idle", "Ready when you are", "Record a message or upload a WAV to begin.");
  healthTimer = setInterval(() => void refreshHealth(), 10000);
  void refreshHealth();
});
