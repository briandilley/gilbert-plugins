import { useMemo } from "react";

import { useWebSocket } from "@/hooks/useWebSocket";

import type { ActiveGame, GameState } from "./types";

interface CreateResult {
  game_id: string;
  join_code: string;
  player_id: string;
  player_token: string;
  state: GameState;
}

interface JoinResult extends CreateResult {}

/** The typed WS RPC bindings returned by ``useMafiaApi``. */
export interface MafiaApi {
  create: (themeKey: string, themeText?: string) => Promise<CreateResult>;
  join: (joinCode: string, name: string) => Promise<JoinResult>;
  resume: (gameId: string, playerToken: string) => Promise<{ state: GameState }>;
  activeGames: () => Promise<{ games: ActiveGame[] }>;
  start: (gameId: string) => Promise<Record<string, unknown>>;
  nightAct: (gameId: string, playerToken: string, targetId: string) => Promise<{ is_killer?: boolean }>;
  vote: (gameId: string, playerToken: string, target: string | null) => Promise<Record<string, unknown>>;
  hostSkip: (gameId: string) => Promise<Record<string, unknown>>;
  hostEndDay: (gameId: string) => Promise<Record<string, unknown>>;
  hostRemove: (gameId: string, playerId: string) => Promise<Record<string, unknown>>;
  hostAbort: (gameId: string) => Promise<Record<string, unknown>>;
}

/** Typed WS RPC bindings for the mafia plugin. */
export function useMafiaApi(): MafiaApi {
  const { rpc } = useWebSocket();
  return useMemo(
    () => ({
      create: (themeKey: string, themeText?: string) =>
        rpc<CreateResult>({ type: "mafia.game.create", theme_key: themeKey, theme_text: themeText ?? "" }),
      join: (joinCode: string, name: string) =>
        rpc<JoinResult>({ type: "mafia.game.join", join_code: joinCode, name }),
      resume: (gameId: string, playerToken: string) =>
        rpc<{ state: GameState }>({ type: "mafia.game.resume", game_id: gameId, player_token: playerToken }),
      activeGames: () => rpc<{ games: ActiveGame[] }>({ type: "mafia.games.active" }),
      start: (gameId: string) =>
        rpc<Record<string, unknown>>({ type: "mafia.game.start", game_id: gameId }, 120_000),
      nightAct: (gameId: string, playerToken: string, targetId: string) =>
        rpc<{ is_killer?: boolean }>(
          { type: "mafia.night.act", game_id: gameId, player_token: playerToken, target_id: targetId },
          120_000,
        ),
      vote: (gameId: string, playerToken: string, target: string | null) =>
        rpc<Record<string, unknown>>(
          { type: "mafia.vote.cast", game_id: gameId, player_token: playerToken, target },
          120_000,
        ),
      hostSkip: (gameId: string) =>
        rpc<Record<string, unknown>>({ type: "mafia.host.skip_phase", game_id: gameId }, 120_000),
      hostEndDay: (gameId: string) =>
        rpc<Record<string, unknown>>({ type: "mafia.host.end_day", game_id: gameId }, 120_000),
      hostRemove: (gameId: string, playerId: string) =>
        rpc<Record<string, unknown>>(
          { type: "mafia.host.remove_player", game_id: gameId, player_id: playerId },
          120_000,
        ),
      hostAbort: (gameId: string) =>
        rpc<Record<string, unknown>>({ type: "mafia.host.abort", game_id: gameId }),
    }),
    [rpc],
  );
}
