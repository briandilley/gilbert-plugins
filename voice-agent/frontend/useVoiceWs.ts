/**
 * useVoiceWs — owns the `/ws/voice/{session_id}` WebSocket for one
 * voice session. Lifecycle:
 *
 *   open(sessionId) → connects WS, sets up audio capture + playback
 *   close()         → tears everything down
 *
 * Audio in (mic → WS): AudioWorklet emits Int16Array buffers;
 * we relay raw bytes as binary WS frames.
 *
 * Audio out (WS → speaker): Pipecat WSTransport serializes audio
 * frames (16-bit LE PCM at session sample rate by default). We
 * decode each frame into a Float32Array and schedule it on a
 * shared AudioContext output node.
 */

import { useCallback, useEffect, useRef } from "react";

export interface UseVoiceWs {
  /** Open the session. Must be called from a user gesture. */
  open(sessionId: string): Promise<void>;
  /** Tear down (idempotent). */
  close(): void;
  /** Live state ref the caller can render against. */
  state: { current: "idle" | "opening" | "open" | "closing" };
}

const TARGET_SAMPLE_RATE = 16000;

export function useVoiceWs(): UseVoiceWs {
  const wsRef = useRef<WebSocket | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const workletRef = useRef<AudioWorkletNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const stateRef = useRef<"idle" | "opening" | "open" | "closing">("idle");

  const close = useCallback((): void => {
    stateRef.current = "closing";
    try { workletRef.current?.disconnect(); } catch { /* already disconnected */ }
    try { sourceRef.current?.disconnect(); } catch { /* already disconnected */ }
    streamRef.current?.getTracks().forEach((t) => t.stop());
    ctxRef.current?.close().catch(() => { /* already closed */ });
    try { wsRef.current?.close(); } catch { /* already closed */ }
    workletRef.current = null;
    sourceRef.current = null;
    streamRef.current = null;
    ctxRef.current = null;
    wsRef.current = null;
    stateRef.current = "idle";
  }, []);

  const open = useCallback(
    async (sessionId: string): Promise<void> => {
      if (stateRef.current !== "idle") return;
      stateRef.current = "opening";
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      streamRef.current = stream;

      const ctx = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
      ctxRef.current = ctx;
      const workletUrl = new URL("./voice-audio-worklet.ts", import.meta.url);
      await ctx.audioWorklet.addModule(workletUrl.href);

      const sourceNode = ctx.createMediaStreamSource(stream);
      sourceRef.current = sourceNode;
      const worklet = new AudioWorkletNode(ctx, "voice-capture");
      workletRef.current = worklet;
      sourceNode.connect(worklet);
      // Worklet output is unused (capture-only); leave unconnected.

      // Open the WS BEFORE wiring port.onmessage so we don't drop
      // the first few buffers.
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${window.location.host}/ws/voice/${sessionId}`);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = (): void => {
        stateRef.current = "open";
        worklet.port.onmessage = (e: MessageEvent<ArrayBuffer>): void => {
          if (ws.readyState === WebSocket.OPEN) ws.send(e.data);
        };
      };
      ws.onmessage = (evt: MessageEvent<ArrayBuffer | string>): void => {
        // Pipecat's WS serializer (default) frames raw 16-bit LE PCM
        // chunks. Decode and schedule onto the playback sink.
        if (typeof evt.data === "string") return; // ignore control frames
        const buf = evt.data as ArrayBuffer;
        const i16 = new Int16Array(buf);
        const f32 = new Float32Array(i16.length);
        for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 0x8000;
        const audioBuf = ctx.createBuffer(1, f32.length, TARGET_SAMPLE_RATE);
        audioBuf.copyToChannel(f32, 0);
        const src = ctx.createBufferSource();
        src.buffer = audioBuf;
        src.connect(ctx.destination);
        src.start();
      };
      ws.onerror = (): void => { close(); };
      ws.onclose = (): void => { close(); };
    },
    [close],
  );

  // Tear down on unmount.
  useEffect(() => close, [close]);

  return { open, close, state: stateRef };
}
