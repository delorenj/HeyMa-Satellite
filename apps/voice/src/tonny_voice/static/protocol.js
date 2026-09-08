import { MAX_WAV_BYTES, checkAbort, validatePCM, validateWav } from "./audio.js";

const CONNECT_WINDOW_MS = 60000;
const ATTEMPT_TIMEOUT_MS = 10000;
const RESPONSE_TIMEOUT_MS = 120000;
const FRAME_BYTES = 2560;

export class VoiceError extends Error {
  constructor(code, retryable = false) { super(code); this.code = code; this.retryable = retryable; }
}

function delay(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer);
      reject(new DOMException("Cancelled", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
  });
}

function attemptTurn(pcm, { signal, onState, sessionId, timeout }) {
  return new Promise((resolve, reject) => {
    checkAbort(signal);
    const url = new URL("/v1/voice", window.location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(url.href);
    socket.binaryType = "arraybuffer";
    let state = "opening";
    let submitted = false;
    let finished = false;
    let bytes = 0;
    const chunks = [];
    const finish = (error, value) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
      try { socket.close(); } catch { /* A failed handshake may already have closed it. */ }
      error ? reject(error) : resolve(value);
    };
    const abort = () => finish(new DOMException("Cancelled", "AbortError"));
    let timer = setTimeout(() => finish(new VoiceError("connection_timeout", true)), timeout);
    signal.addEventListener("abort", abort, { once: true });
    socket.onopen = () => {
      state = "hello";
      try {
        socket.send(JSON.stringify({
          type: "hello", session_id: sessionId, sample_rate: 16000,
          encoding: "pcm_s16le", channels: 1, client: "tonny", version: "0.1.0",
        }));
      } catch {
        finish(new VoiceError("connection_lost", true));
      }
    };
    socket.onerror = socket.onclose = () => finish(new VoiceError("connection_lost", !submitted));
    socket.onmessage = ({ data }) => {
      try {
        if (data instanceof ArrayBuffer) {
          if (state !== "receiving" || !data.byteLength) throw new VoiceError("invalid_response");
          bytes += data.byteLength;
          if (bytes > MAX_WAV_BYTES) throw new VoiceError("response_too_large");
          chunks.push(new Uint8Array(data));
          return;
        }
        if (typeof data !== "string" || new TextEncoder().encode(data).length > 4096) {
          throw new VoiceError("invalid_response");
        }
        const control = JSON.parse(data);
        if (!control || typeof control !== "object" || Array.isArray(control)) {
          throw new VoiceError("invalid_response");
        }
        if (control.type === "error") {
          // Never expose arbitrary provider/server message text in the browser.
          const known = ["busy", "no_speech", "not_configured", "timeout", "internal", "input_too_large",
            "frame_too_large", "invalid_audio", "invalid_state", "protocol_error",
            "provider_error", "stt_failed", "llm_failed", "tts_failed", "empty_answer", "empty_audio", "response_too_large"];
          throw new VoiceError(known.includes(control.code) ? control.code : "gateway_error");
        }
        if (control.type === "ready" && state === "hello" && control.session_id === sessionId) {
          clearTimeout(timer);
          timer = setTimeout(() => finish(new VoiceError("response_timeout")), RESPONSE_TIMEOUT_MS);
          state = "waiting_response";
          // Crossing this line prohibits retries, even if the first send throws.
          submitted = true;
          for (let offset = 0; offset < pcm.length; offset += FRAME_BYTES) {
            socket.send(pcm.subarray(offset, offset + FRAME_BYTES));
          }
          socket.send(JSON.stringify({ type: "end_of_input" }));
          onState("processing");
          return;
        }
        if (control.type === "response_start" && state === "waiting_response"
          && control.format === "wav" && control.final === true) {
          state = "receiving";
          return;
        }
        if (control.type === "response_end" && state === "receiving" && bytes) {
          const wav = new Uint8Array(bytes);
          let offset = 0;
          for (const chunk of chunks) { wav.set(chunk, offset); offset += chunk.length; }
          validateWav(wav);
          finish(null, wav);
          return;
        }
        throw new VoiceError("invalid_response");
      } catch (error) {
        finish(error instanceof VoiceError ? error : new VoiceError("invalid_response"));
      }
    };
    if (signal.aborted) abort();
  });
}

export async function submitTurn(pcm, { signal, onState }) {
  validatePCM(pcm);
  const sessionId = crypto.randomUUID();
  const deadline = performance.now() + CONNECT_WINDOW_MS;
  let backoff = 1000;
  let attempt = 0;
  while (performance.now() < deadline) {
    checkAbort(signal);
    onState("connecting", ++attempt);
    try {
      return await attemptTurn(pcm, {
        signal, onState, sessionId, timeout: Math.min(ATTEMPT_TIMEOUT_MS, deadline - performance.now()),
      });
    } catch (error) {
      checkAbort(signal);
      if (!error.retryable) throw error;
      const remaining = deadline - performance.now();
      if (remaining <= 0) break;
      onState("reconnecting", attempt);
      await delay(Math.min(backoff, remaining), signal);
      backoff = Math.min(backoff * 2, 5000);
    }
  }
  throw new VoiceError("connection_unavailable");
}
