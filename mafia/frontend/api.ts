import { useMemo } from "react";

import { useWebSocket } from "@/hooks/useWebSocket";

import type { ActiveGame, GameState, SpeakersResponse } from "./types";

interface CreateResult {
  game_id: string;
  join_code: string;
  player_id: string;
  player_token: string;
  state: GameState;
}

interface JoinResult extends CreateResult {}

/** Per-game narration output the host picks when creating a game. */
export interface NarrationChoice {
  speakerNames: string[];
  volume: number;
}

/** The typed WS RPC bindings returned by ``useMafiaApi``. */
export interface MafiaApi {
  listSpeakers: () => Promise<SpeakersResponse>;
  create: (themeKey: string, themeText: string, narration: NarrationChoice) => Promise<CreateResult>;
  join: (joinCode: string, name: string) => Promise<JoinResult>;
  resume: (gameId: string, playerToken: string) => Promise<{ state: GameState }>;
  activeGames: () => Promise<{ games: ActiveGame[] }>;
  start: (gameId: string) => Promise<Record<string, unknown>>;
  nightAct: (
    gameId: string,
    playerToken: string,
    action: "kill" | "save" | "check" | "ready",
    targetId?: string,
  ) => Promise<{ is_killer?: boolean }>;
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
      listSpeakers: () => rpc<SpeakersResponse>({ type: "mafia.speakers.list" }),
      create: (themeKey: string, themeText: string, narration: NarrationChoice) =>
        rpc<CreateResult>({
          type: "mafia.game.create",
          theme_key: themeKey,
          theme_text: themeText,
          // Omit speaker_names when none are chosen so the backend falls
          // back to the default announce speakers.
          speaker_names: narration.speakerNames.length ? narration.speakerNames : undefined,
          volume: narration.volume,
        }),
      join: (joinCode: string, name: string) =>
        rpc<JoinResult>({ type: "mafia.game.join", join_code: joinCode, name }),
      resume: (gameId: string, playerToken: string) =>
        rpc<{ state: GameState }>({ type: "mafia.game.resume", game_id: gameId, player_token: playerToken }),
      activeGames: () => rpc<{ games: ActiveGame[] }>({ type: "mafia.games.active" }),
      start: (gameId: string) =>
        rpc<Record<string, unknown>>({ type: "mafia.game.start", game_id: gameId }, 120_000),
      nightAct: (
        gameId: string,
        playerToken: string,
        action: "kill" | "save" | "check" | "ready",
        targetId?: string,
      ) =>
        rpc<{ is_killer?: boolean }>(
          {
            type: "mafia.night.act",
            game_id: gameId,
            player_token: playerToken,
            action,
            target_id: targetId,
          },
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
