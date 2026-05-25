/**
 * Voice-agent page — a single "Start Conversation" toggle.
 *
 * Audio capture and the binary WebSocket to the voice pipeline are
 * handled entirely by ``useVoiceWs``. This component is responsible
 * for:
 *
 *   1. Calling ``voice.start_session`` over the RPC WebSocket to
 *      reserve a session_id on the server.
 *   2. Handing the session_id to ``useVoiceWs().open()`` so the
 *      dedicated binary WS opens to ``/ws/voice/{session_id}``.
 *   3. Subscribing to ``voice.*`` events for transcript updates,
 *      session-ended notifications, and state transitions.
 *
 * Outbound audio (Gilbert's TTS) is handled by the voice pipeline
 * itself — Pipecat pushes synthesized PCM over the same binary WS
 * and ``useVoiceWs`` schedules it onto the AudioContext destination.
 */

import { useCallback, useEffect, useRef, useState, type ReactElement } from "react";
import { Mic, MicOff, Loader2, Ear } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useVoiceWs } from "./useVoiceWs";

/** A single transcript turn rendered in the live feed. */
interface TranscriptTurn {
  who: string;       // "us" (Gilbert) | "them" (the user) | "system"
  text: string;
  ts: number;        // seconds since session start (from server)
  /** Wall-clock epoch millis captured at SPA receive time. Used to
   * render a HH:MM:SS column so we can spot turn-queue weirdness
   * (e.g. user repeating themselves because the first attempt
   * looked stuck, then both attempts processing back-to-back). */
  receivedAt: number;
  /** Local React-only id so we can render this without a key collision when
   * the same text repeats. The server doesn't issue ids; we mint per-row. */
  key: string;
}

/** Format epoch ms as "HH:MM:SS" in the user's local timezone. */
function formatWallClock(epochMs: number): string {
  const d = new Date(epochMs);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

type SessionState =
  | "idle"
  | "starting"
  | "active"      // engine listening + responding
  | "dormant"     // conversational-mode only: waiting for "Hey Gilbert"
  | "stopping";

type SessionMode = "turn_based" | "conversational";

export function VoiceAgentPage(): ReactElement {
  const { connected, rpc, subscribe } = useWebSocket();
  const voiceWs = useVoiceWs();

  const [state, setState] = useState<SessionState>("idle");
  const [mode, setMode] = useState<SessionMode>("turn_based");
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptTurn[]>([]);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);

  // Subscribe to live transcript-turn events from the backend. The
  // server emits ``voice.transcript_turn`` for every "them"
  // (user-side STT commit) and "us" (LLM reply) turn so the SPA can
  // render the conversation as it happens — useful even when the
  // audio-out path is misbehaving and the user can't hear Gilbert.
  useEffect(() => {
    const unsub = subscribe("voice.transcript_turn", (event) => {
      const data = event.data ?? {};
      const who = String(data.who ?? "");
      const text = String(data.text ?? "");
      const ts =
        typeof data.ts === "number" ? data.ts : Number(data.ts ?? 0);
      if (!who || !text) return;
      const receivedAt = Date.now();
      const newTurn: TranscriptTurn = {
        who,
        text,
        ts,
        receivedAt,
        key: `${ts}-${who}-${Math.random().toString(36).slice(2, 8)}`,
      };
      setTranscript((prev) => [...prev, newTurn]);
    });
    return unsub;
  }, [subscribe]);

  // Subscribe to "session ended" so the SPA can flip back to idle
  // when the brain decides the conversation is over (e.g. the user
  // said "talk to you later" and the LLM called end_conversation).
  // Without this the SPA stays in active mode with the WS open and
  // audio streaming that nothing's listening to.
  useEffect(() => {
    const unsub = subscribe("voice.session_ended", () => {
      voiceWs.close();
      setSessionId(null);
      setState("idle");
    });
    return unsub;
  }, [subscribe, voiceWs]);

  // Conversational mode emits ``voice.state_changed`` when the
  // server transitions between ``active`` (engine listening) and
  // ``dormant`` (only the wake-word detector listening, waiting for
  // "Hey Gilbert" to resume). Reflect the state in the UI so the
  // user knows what's going on. The WS stays open in both states —
  // only the routing on the server side changes.
  useEffect(() => {
    const unsub = subscribe("voice.state_changed", (event) => {
      const data = event.data ?? {};
      const newState = String(data.state ?? "");
      if (newState === "dormant") {
        setState("dormant");
      } else if (newState === "active") {
        setState("active");
      }
    });
    return unsub;
  }, [subscribe]);

  // Auto-scroll the transcript on every new turn.
  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [transcript]);

  const stop = useCallback(async () => {
    // ``dormant`` is a perfectly valid state to stop from too —
    // conversational mode sits there waiting for "Hey Gilbert" but
    // the user might want to hard-end without doing the dance.
    if (state !== "active" && state !== "dormant") return;
    setState("stopping");
    // Close the binary WS first for fast local mic teardown.
    voiceWs.close();
    if (sessionId) {
      try {
        await rpc<{ ok: boolean }>({
          type: "voice.end_session",
          session_id: sessionId,
        });
      } catch {
        /* server may already be torn down */
      }
    }
    setSessionId(null);
    setState("idle");
  }, [state, sessionId, rpc, voiceWs]);

  const start = useCallback(async () => {
    if (state !== "idle") return;
    setError(null);
    setTranscript([]);
    setState("starting");

    // Step 1: reserve the session server-side. The server allocates a
    // session_id and holds a slot in the voice pipeline. The binary WS
    // must open AFTER this succeeds so the session exists before the
    // transport tries to attach to it.
    let resp: { ok: boolean; session_id?: string; error?: string };
    try {
      resp = await rpc({ type: "voice.start_session", mode });
    } catch (err) {
      setError(
        err instanceof Error
          ? `Failed to start session: ${err.message}`
          : "Failed to start session"
      );
      setState("idle");
      return;
    }
    if (!resp.ok || !resp.session_id) {
      setError(resp.error ?? "Server refused the session");
      setState("idle");
      return;
    }
    const newSessionId = resp.session_id;
    setSessionId(newSessionId);

    // Step 2: open the dedicated binary WS. This acquires the mic,
    // spins up the AudioWorklet, and connects to /ws/voice/{session_id}.
    try {
      await voiceWs.open(newSessionId);
    } catch (err) {
      setError(
        err instanceof Error
          ? `Microphone / audio setup failed: ${err.message}`
          : "Microphone / audio setup failed"
      );
      // Best-effort cleanup — end the server-side session we already reserved.
      try {
        await rpc({ type: "voice.end_session", session_id: newSessionId });
      } catch {
        /* ignore */
      }
      setSessionId(null);
      setState("idle");
      return;
    }

    // Step 3: flip UI to the right post-start state.
    setState(mode === "conversational" ? "dormant" : "active");
  }, [state, mode, rpc, voiceWs]);

  return (
    <div className="container mx-auto max-w-2xl py-8">
      <h1 className="text-2xl font-semibold mb-2">Voice conversation</h1>
      <p className="text-muted-foreground mb-6">
        Start a real-time voice conversation with Gilbert. The mic
        captures locally; Gilbert speaks back through this tab.
      </p>

      <Card className="p-6 flex flex-col items-center gap-4">
        {state === "idle" && (
          <>
            <div className="flex gap-2" role="radiogroup" aria-label="Mode">
              <Button
                size="sm"
                variant={mode === "turn_based" ? "default" : "outline"}
                onClick={() => setMode("turn_based")}
                role="radio"
                aria-checked={mode === "turn_based"}
              >
                Turn-based
              </Button>
              <Button
                size="sm"
                variant={mode === "conversational" ? "default" : "outline"}
                onClick={() => setMode("conversational")}
                role="radio"
                aria-checked={mode === "conversational"}
              >
                Conversational
              </Button>
            </div>
            <p className="text-xs text-muted-foreground max-w-md text-center">
              {mode === "turn_based"
                ? "Press the button, talk; Gilbert speaks back. Stays open until you (or he) ends it."
                : "Like turn-based, but after 10 seconds of silence the session drops to wake-word mode. Say “Hey Gilbert” to wake him up again."}
            </p>
            <Button size="lg" onClick={start} disabled={!connected}>
              <Mic className="mr-2 h-5 w-5" />
              Start Conversation
            </Button>
          </>
        )}
        {state === "starting" && (
          <Button size="lg" disabled>
            <Loader2 className="mr-2 h-5 w-5 animate-spin" />
            Starting…
          </Button>
        )}
        {state === "active" && (
          <Button size="lg" variant="destructive" onClick={stop}>
            <MicOff className="mr-2 h-5 w-5" />
            End Conversation
          </Button>
        )}
        {state === "dormant" && (
          <Button size="lg" variant="destructive" onClick={stop}>
            <MicOff className="mr-2 h-5 w-5" />
            End Conversation
          </Button>
        )}
        {state === "stopping" && (
          <Button size="lg" disabled>
            <Loader2 className="mr-2 h-5 w-5 animate-spin" />
            Stopping…
          </Button>
        )}

        {state === "active" && (
          <p className="text-sm text-muted-foreground">
            Listening… speak naturally. Gilbert will respond by voice.
          </p>
        )}
        {state === "dormant" && (
          <p className="flex items-center gap-2 text-sm font-medium text-amber-600 dark:text-amber-400">
            <Ear className="h-4 w-4" />
            Waiting for &ldquo;Hey Gilbert&rdquo; — say the wake phrase to resume.
          </p>
        )}
        {error && (
          <p className="text-sm text-destructive font-medium">{error}</p>
        )}
        {!connected && (
          <p className="text-sm text-muted-foreground">
            Reconnecting to Gilbert…
          </p>
        )}
      </Card>

      {transcript.length > 0 && (
        <Card className="mt-6 p-4 max-h-[400px] overflow-y-auto">
          <h2 className="text-sm font-semibold text-muted-foreground mb-3">
            Live transcript
          </h2>
          <div className="space-y-2 text-sm">
            {transcript.map((t) => (
              <div
                key={t.key}
                className={
                  t.who === "us"
                    ? "flex items-start gap-3"
                    : t.who === "them"
                      ? "flex items-start gap-3"
                      : "flex items-start gap-3 text-muted-foreground italic"
                }
              >
                {/* Trailing spaces on the timestamp + label spans
                    are intentional — the visual ``flex gap-3``
                    doesn't make it into clipboard text, so a copy
                    would otherwise read "20:56:49GilbertHey there"
                    with everything mashed together. The string-
                    literal form ``{`…: `}`` ensures the trailing
                    whitespace survives JSX tokenization. */}
                <span
                  className="shrink-0 font-mono text-xs text-muted-foreground w-20 pt-0.5"
                  title={`Received ${new Date(t.receivedAt).toLocaleString()} · session t=${t.ts.toFixed(2)}s`}
                >
                  {`${formatWallClock(t.receivedAt)} `}
                </span>
                <span
                  className={
                    "shrink-0 font-semibold w-20 " +
                    (t.who === "us"
                      ? "text-primary"
                      : t.who === "them"
                        ? "text-foreground"
                        : "text-muted-foreground")
                  }
                >
                  {`${
                    t.who === "us"
                      ? "Gilbert"
                      : t.who === "them"
                        ? "You"
                        : "System"
                  }: `}
                </span>
                <span className="flex-1 break-words">{t.text}</span>
              </div>
            ))}
            <div ref={transcriptEndRef} />
          </div>
        </Card>
      )}

      <div className="mt-6 text-xs text-muted-foreground space-y-1">
        <p>
          Tips: keep the browser tab focused, allow microphone access
          when prompted, and use a headset for the cleanest pickup.
        </p>
        <p>
          This is v1 — turn-taking only. Real-time barge-in (cutting
          Gilbert off mid-sentence) requires a different audio path
          and is coming next.
        </p>
      </div>
    </div>
  );
}
