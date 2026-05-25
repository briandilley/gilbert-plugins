/**
 * AudioWorklet — captures the microphone at the AudioContext's native
 * sample rate (typically 48 kHz) and downsamples to 16 kHz s16 PCM,
 * posting Int16Array buffers back to the main thread via `port`.
 *
 * The main thread relays each Int16Array as a binary WS frame to
 * `/ws/voice/{session_id}`. Pipecat's WS transport interprets raw
 * 16-bit little-endian PCM at the negotiated sample rate.
 *
 * This file is emitted by Vite as a separate standalone asset (via
 * `import workletUrl from "./voice-audio-worklet.js?url"` in
 * useVoiceWs.ts) so that `AudioContext.audioWorklet.addModule(url)`
 * can load it directly. The main bundle cannot be loaded as a worklet
 * module — it must be a separate JS file.
 *
 * It runs in a dedicated audio thread (`AudioWorkletGlobalScope`) and
 * must not import anything from the main SPA bundle.
 */

const TARGET_SAMPLE_RATE = 16000;

class VoiceCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._ratio = sampleRate / TARGET_SAMPLE_RATE;
  }

  process(inputs, _outputs) {
    const input = inputs[0]?.[0];
    if (!input || input.length === 0) return true;
    const outLen = Math.floor(input.length / this._ratio);
    const out = new Int16Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const start = Math.floor(i * this._ratio);
      const end = Math.floor((i + 1) * this._ratio);
      let sum = 0;
      let n = 0;
      for (let j = start; j < end && j < input.length; j++) {
        sum += input[j];
        n++;
      }
      const v = n > 0 ? sum / n : 0;
      const clamped = Math.max(-1, Math.min(1, v));
      out[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    }
    this.port.postMessage(out.buffer, [out.buffer]);
    return true;
  }
}

registerProcessor("voice-capture", VoiceCaptureProcessor);
