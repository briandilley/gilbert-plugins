/**
 * AudioWorklet — captures the microphone at the AudioContext's native
 * sample rate (typically 48 kHz) and downsamples to 16 kHz s16 PCM,
 * posting Int16Array buffers back to the main thread via `port`.
 *
 * The main thread relays each Int16Array as a binary WS frame to
 * `/ws/voice/{session_id}`. Pipecat's WS transport interprets raw
 * 16-bit little-endian PCM at the negotiated sample rate.
 *
 * This file is loaded as an AudioWorklet module via
 * `ctx.audioWorklet.addModule(new URL("./voice-audio-worklet.ts", import.meta.url).href)`.
 * It runs in a dedicated audio thread (`AudioWorkletGlobalScope`) and must
 * not import anything from the main SPA bundle.
 *
 * Type annotations: `AudioWorkletProcessor`, `registerProcessor`, and the
 * global `sampleRate` live in `AudioWorkletGlobalScope`, not `Window`.
 * TypeScript's `lib.dom.d.ts` does not declare them, so we declare the
 * minimal shapes needed here rather than pollute the main tsconfig.
 */

// ---------------------------------------------------------------------------
// Minimal AudioWorkletGlobalScope type declarations
// (not in lib.dom.d.ts; they exist only inside the worklet thread).
// ---------------------------------------------------------------------------

declare const sampleRate: number;

declare abstract class AudioWorkletProcessor {
  readonly port: MessagePort;
  abstract process(
    inputs: Float32Array[][],
    outputs: Float32Array[][],
    parameters: Record<string, Float32Array>,
  ): boolean;
}

declare function registerProcessor(
  name: string,
  processorCtor: new () => AudioWorkletProcessor,
): void;

// ---------------------------------------------------------------------------
// Processor implementation
// ---------------------------------------------------------------------------

const TARGET_SAMPLE_RATE = 16000;

class VoiceCaptureProcessor extends AudioWorkletProcessor {
  private _ratio = sampleRate / TARGET_SAMPLE_RATE;

  process(
    inputs: Float32Array[][],
    _outputs: Float32Array[][],
  ): boolean {
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

// Required by `isolatedModules: true` — make this file a module.
export {};
