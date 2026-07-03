import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactElement } from "react";

import { useWebSocket } from "@/hooks/useWebSocket";

import { useMafiaApi } from "./api";
import { CharacterCard } from "./components/CharacterCard";
import { GhostPanel } from "./components/GhostPanel";
import { HostControls } from "./components/HostControls";
import { JoinGate } from "./components/JoinGate";
import { Lobby } from "./components/Lobby";
import { NightAction } from "./components/NightAction";
import { StoryLog } from "./components/StoryLog";
import { VotePanel } from "./components/VotePanel";
import type { ActiveGame, GameState, MafiaSession, MafiaStateFrame } from "./types";

const SESSION_KEY = "mafia.session";

function loadSession(): MafiaSession | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    return raw ? (JSON.parse(raw) as MafiaSession) : null;
  } catch {
    return null;
  }
}

/** The /mafia page: join gate → lobby → night/day play → ghost/finale. */
export function MafiaPage(): ReactElement {
  const api = useMafiaApi();
  const { subscribe, connected } = useWebSocket();
  const [session, setSession] = useState<MafiaSession | null>(loadSession);
  const [state, setState] = useState<GameState | null>(null);
  const [activeGames, setActiveGames] = useState<ActiveGame[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // canCreate: probe once — guests get a 403 creating, so gate on /auth/me
  const [canCreate, setCanCreate] = useState(false);

  // live pushes — the server enqueues mafia.state as a direct frame (not a
  // gilbert.event envelope), so the subscriber receives the raw frame.
  useEffect(
    () =>
      subscribe("mafia.state", (frame) => {
        const f = frame as unknown as MafiaStateFrame;
        if (!session || f.game_id === session.gameId) setState(f.state);
      }),
    [subscribe, session],
  );

  // resume or browse on connect
  useEffect(() => {
    if (!connected) return;
    void (async () => {
      if (session) {
        try {
          const { state: s } = await api.resume(session.gameId, session.token);
          setState(s);
          return;
        } catch {
          localStorage.removeItem(SESSION_KEY);
          setSession(null);
        }
      }
      try {
        const { games } = await api.activeGames();
        setActiveGames(games);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, [connected]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void fetch("/auth/me")
      .then((r) => r.json())
      .then((u: { user_id?: string }) =>
        setCanCreate(Boolean(u.user_id) && u.user_id !== "guest" && u.user_id !== "system"),
      )
      .catch(() => setCanCreate(false));
  }, []);

  const adopt = useCallback((gameId: string, playerId: string, token: string, s: GameState) => {
    const next = { gameId, playerId, token };
    localStorage.setItem(SESSION_KEY, JSON.stringify(next));
    setSession(next);
    setState(s);
    setError(null);
  }, []);

  const run = useCallback(async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  const handlers = useMemo(
    () =>
      session && state
        ? {
            onStart: () => run(() => api.start(session.gameId)),
            onSubmit: (action: "kill" | "save" | "check" | "ready", t?: string) =>
              run(() => api.nightAct(session.gameId, session.token, action, t)),
            onVote: (t: string | null) => run(() => api.vote(session.gameId, session.token, t)),
            onSkip: () => run(() => api.hostSkip(session.gameId)),
            onEndDay: () => run(() => api.hostEndDay(session.gameId)),
            onRemove: (p: string) => run(() => api.hostRemove(session.gameId, p)),
            onAbort: () =>
              run(async () => {
                await api.hostAbort(session.gameId);
                localStorage.removeItem(SESSION_KEY);
                setSession(null);
                setState(null);
              }),
          }
        : null,
    [api, run, session, state],
  );

  if (!session || !state || !handlers) {
    return (
      <JoinGate
        activeGames={activeGames}
        canCreate={canCreate}
        error={error}
        onJoin={(code, name) =>
          run(async () => {
            const r = await api.join(code, name);
            adopt(r.game_id, r.player_id, r.player_token, r.state);
          })
        }
        onCreate={(themeKey, themeText, narration) =>
          run(async () => {
            const r = await api.create(themeKey, themeText, narration);
            adopt(r.game_id, r.player_id, r.player_token, r.state);
          })
        }
      />
    );
  }

  const { you } = state;
  return (
    <div className="mx-auto flex max-w-md flex-col gap-4 p-4">
      {error && <div className="text-sm text-red-400">{error}</div>}
      {state.phase === "lobby" && <Lobby state={state} onStart={handlers.onStart} />}
      {state.phase !== "lobby" && <CharacterCard you={you} />}
      {!you.alive || state.winner ? (
        <GhostPanel state={state} />
      ) : (
        <>
          {state.phase === "night" && (
            <NightAction state={state} onSubmit={handlers.onSubmit} busy={busy} />
          )}
          {state.phase === "day" && (
            <VotePanel state={state} onVote={handlers.onVote} busy={busy} />
          )}
          <StoryLog story={state.story} />
        </>
      )}
      <HostControls
        state={state}
        onSkip={handlers.onSkip}
        onEndDay={handlers.onEndDay}
        onRemove={handlers.onRemove}
        onAbort={handlers.onAbort}
      />
    </div>
  );
}
