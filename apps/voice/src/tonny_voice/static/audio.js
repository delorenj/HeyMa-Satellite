// This boundary intentionally matches apps/satellite/satellite.py.
export const SAMPLE_RATE = 16000;
export const MAX_PCM_BYTES = SAMPLE_RATE * 2 * 15;
export const MAX_WAV_BYTES = 4 * 1024 * 1024;

export class AudioError extends Error {
  constructor(code) { super(code); this.code = code; }
}

export function checkAbort(signal) {
  if (signal.aborted) throw new DOMException("Cancelled", "AbortError");
}

export function validatePCM(pcm) {
  if (!(pcm instanceof Uint8Array) || !pcm.length || pcm.length % 2) {
    throw new AudioError("invalid_pcm_samples");
  }
  if (pcm.length > MAX_PCM_BYTES) throw new AudioError("capture_too_large");
}

export function validateWav(value, { capture = false } = {}) {
  const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const tag = (offset) => String.fromCharCode(...bytes.subarray(offset, offset + 4));
  if (bytes.length > MAX_WAV_BYTES) throw new AudioError("wav_too_large");
  if (bytes.length < 44 || tag(0) !== "RIFF" || tag(8) !== "WAVE") {
    throw new AudioError("invalid_wav_header");
  }
  if (view.getUint32(4, true) + 8 !== bytes.length) throw new AudioError("invalid_wav_length");
  let format;
  let audio;
  for (let offset = 12; offset < bytes.length;) {
    if (offset + 8 > bytes.length) throw new AudioError("truncated_wav_chunk");
    const kind = tag(offset);
    const size = view.getUint32(offset + 4, true);
    const body = offset + 8;
    const next = body + size + (size % 2);
    if (next > bytes.length) throw new AudioError("truncated_wav_chunk");
    if (kind === "fmt ") {
      if (format || size < 16) throw new AudioError("invalid_wav_format");
      format = {
        encoding: view.getUint16(body, true), channels: view.getUint16(body + 2, true),
        sampleRate: view.getUint32(body + 4, true), byteRate: view.getUint32(body + 8, true),
        blockAlign: view.getUint16(body + 12, true), bits: view.getUint16(body + 14, true),
      };
    } else if (kind === "data") {
      if (audio) throw new AudioError("duplicate_wav_data");
      audio = { offset: body, size };
    }
    offset = next;
  }
  if (!format || !audio) throw new AudioError("missing_wav_chunks");
  const { encoding, channels, sampleRate, byteRate, blockAlign, bits } = format;
  if (encoding !== 1 || ![1, 2].includes(channels) || ![8, 16, 24, 32].includes(bits)) {
    throw new AudioError("unsupported_wav_format");
  }
  if (sampleRate < 8000 || sampleRate > 192000) throw new AudioError("unsupported_wav_rate");
  if (blockAlign !== channels * bits / 8 || byteRate !== sampleRate * blockAlign) {
    throw new AudioError("invalid_wav_alignment");
  }
  if (!audio.size || audio.size % blockAlign) throw new AudioError("invalid_wav_frames");
  if (capture) {
    if (sampleRate !== SAMPLE_RATE || channels !== 1 || bits !== 16) {
      throw new AudioError("input_wav_must_be_16000hz_mono_pcm16");
    }
    if (audio.size > MAX_PCM_BYTES) throw new AudioError("capture_too_large");
  }
  return {
    sampleRate, channels, bits, seconds: audio.size / byteRate,
    pcm: bytes.subarray(audio.offset, audio.offset + audio.size),
  };
}

export function pcmToWav(pcm) {
  validatePCM(pcm);
  const wav = new Uint8Array(44 + pcm.length);
  const view = new DataView(wav.buffer);
  const writeTag = (offset, text) => wav.set(new TextEncoder().encode(text), offset);
  writeTag(0, "RIFF"); view.setUint32(4, wav.length - 8, true); writeTag(8, "WAVE");
  writeTag(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, SAMPLE_RATE, true);
  view.setUint32(28, SAMPLE_RATE * 2, true); view.setUint16(32, 2, true);
  view.setUint16(34, 16, true); writeTag(36, "data"); view.setUint32(40, pcm.length, true);
  wav.set(pcm, 44);
  return wav;
}

// Resample through Web Audio's filter, rather than dropping samples and aliasing speech.
export async function resamplePCM(samples, inputRate) {
  if (!samples.length) throw new AudioError("empty_capture");
  let mono = samples;
  if (inputRate !== SAMPLE_RATE) {
    const length = Math.max(1, Math.round(samples.length * SAMPLE_RATE / inputRate));
    const offline = new OfflineAudioContext(1, length, SAMPLE_RATE);
    const buffer = offline.createBuffer(1, samples.length, inputRate);
    buffer.copyToChannel(samples, 0);
    const source = offline.createBufferSource();
    source.buffer = buffer;
    source.connect(offline.destination);
    source.start();
    mono = (await offline.startRendering()).getChannelData(0);
  }
  const pcm = new Uint8Array(mono.length * 2);
  const view = new DataView(pcm.buffer);
  for (let index = 0; index < mono.length; index++) {
    const sample = Math.max(-1, Math.min(1, Number.isFinite(mono[index]) ? mono[index] : 0));
    view.setInt16(index * 2, Math.round(sample * (sample < 0 ? 32768 : 32767)), true);
  }
  validatePCM(pcm);
  return pcm;
}

function stopTracks(stream) { stream?.getTracks().forEach((track) => track.stop()); }

function waitFor(promise, signal, timeoutMs, code, onLateValue = () => {}) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      error ? reject(error) : resolve(value);
    };
    const abort = () => finish(new DOMException("Cancelled", "AbortError"));
    const timer = setTimeout(() => finish(new AudioError(code)), timeoutMs);
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
    Promise.resolve(promise).then(
      (value) => settled ? onLateValue(value) : finish(null, value),
      (error) => finish(error),
    );
  });
}

export async function captureMicrophone({ seconds = 6, signal, onReady, onProgress }) {
  checkAbort(signal);
  if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 15) {
    throw new AudioError("invalid_capture_duration");
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext || !window.AudioWorkletNode) {
    throw new AudioError("microphone_unavailable");
  }
  // Start/resume in the click handler's activation window, before permission awaits.
  const context = new AudioContext();
  let stream;
  let source;
  let node;
  let disposed = false;
  const dispose = () => {
    // Permission may resolve in the same microtask turn as cancellation.
    stopTracks(stream);
    if (disposed) return;
    disposed = true;
    source?.disconnect();
    node?.disconnect();
    node?.port.close();
    if (context.state !== "closed") void context.close().catch(() => {});
  };
  signal.addEventListener("abort", dispose, { once: true });
  try {
    await waitFor(context.resume(), signal, 5000, "microphone_unavailable");
    checkAbort(signal);
    const permission = navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      video: false,
    });
    // Ignored prompts have a deadline; a stream granted after cancel/timeout is stopped.
    stream = await waitFor(permission, signal, 20000, "microphone_permission_timeout", stopTracks);
    checkAbort(signal);
    await waitFor(context.audioWorklet.addModule("/static/capture-worklet.js"), signal, 10000, "microphone_unavailable");
    checkAbort(signal);
    const inputRate = context.sampleRate;
    const maxFrames = Math.floor(seconds * inputRate);
    node = new AudioWorkletNode(context, "tonny-capture", {
      numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1, channelCountMode: "explicit",
      processorOptions: { maxFrames },
    });
    source = context.createMediaStreamSource(stream);
    const samples = await new Promise((resolve, reject) => {
      const chunks = [];
      let frames = 0;
      let finished = false;
      let stopTimer;
      const tracks = stream.getAudioTracks();
      const done = (error) => {
        if (finished) return;
        finished = true;
        clearTimeout(watchdog);
        clearTimeout(stopTimer);
        signal.removeEventListener("abort", abort);
        tracks.forEach((track) => track.removeEventListener("ended", ended));
        node.port.onmessage = null;
        node.onprocessorerror = null;
        if (error) { reject(error); return; }
        const joined = new Float32Array(frames);
        let offset = 0;
        for (const chunk of chunks) { joined.set(chunk, offset); offset += chunk.length; }
        resolve(joined);
      };
      const abort = () => done(new DOMException("Cancelled", "AbortError"));
      const ended = () => done(new AudioError("microphone_disconnected"));
      const watchdog = setTimeout(() => done(new AudioError("capture_timeout")), seconds * 1000 + 5000);
      signal.addEventListener("abort", abort, { once: true });
      tracks.forEach((track) => track.addEventListener("ended", ended, { once: true }));
      node.onprocessorerror = () => done(new AudioError("capture_failed"));
      node.port.onmessage = ({ data }) => {
        if (finished) return;
        if (data.type === "samples") {
          const chunk = new Float32Array(data.samples);
          if (frames + chunk.length > maxFrames) { done(new AudioError("capture_too_large")); return; }
          chunks.push(chunk);
          frames += chunk.length;
          onProgress(frames / inputRate);
        } else if (data.type === "done") {
          done();
        }
      };
      source.connect(node);
      node.connect(context.destination); // The processor writes silence, never microphone monitoring.
      onReady(() => {
        if (finished || stopTimer) return;
        node.port.postMessage({ type: "stop" });
        stopTimer = setTimeout(() => done(new AudioError("capture_timeout")), 2000);
      });
      if (signal.aborted) abort();
    });
    dispose(); // Release the physical mic before resampling or contacting the gateway.
    checkAbort(signal);
    const pcm = await resamplePCM(samples, inputRate);
    checkAbort(signal);
    return pcm;
  } finally {
    signal.removeEventListener("abort", dispose);
    dispose();
  }
}
