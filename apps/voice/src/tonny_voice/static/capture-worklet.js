class TonnyCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.remaining = options.processorOptions.maxFrames;
    this.pending = new Float32Array(1024);
    this.used = 0;
    this.stopped = false;
    this.port.onmessage = ({ data }) => { if (data.type === "stop") this.stop(); };
  }

  flush() {
    if (!this.used) return;
    const samples = this.pending.slice(0, this.used);
    this.port.postMessage({ type: "samples", samples: samples.buffer }, [samples.buffer]);
    this.used = 0;
  }

  stop() {
    if (this.stopped) return;
    this.stopped = true;
    this.flush();
    this.port.postMessage({ type: "done" });
  }

  process(inputs) {
    if (this.stopped) return false;
    const channels = inputs[0];
    if (!channels.length) return true;
    const count = Math.min(channels[0].length, this.remaining);
    for (let index = 0; index < count; index++) {
      let mono = 0;
      for (const channel of channels) mono += channel[index] / channels.length;
      this.pending[this.used++] = mono;
      if (this.used === this.pending.length) this.flush();
    }
    this.remaining -= count;
    if (!this.remaining) this.stop();
    return !this.stopped;
  }
}

registerProcessor("tonny-capture", TonnyCapture);
