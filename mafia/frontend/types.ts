/** Wire types for the mafia.* WS protocol — must mirror game.state_for(). */

export type PhaseKey =
  | "lobby"
  | "night_killers"
  | "night_doctor"
  | "night_detective"
  | "dawn"
  | "day"
  | "dusk"
  | "ended";

export type CharacterKey = "citizen" | "killer" | "doctor" | "detective";

export interface PlayerSummary {
  player_id: string;
  name: string;
  alive: boolean;
  is_host: boolean;
  character: CharacterKey | null;
}

export interface CheckResult {
  player_id: string;
  name: string;
  is_killer: boolean;
}

export interface YouState {
  player_id: string;
  name: string;
  alive: boolean;
  is_host: boolean;
  character: CharacterKey | null;
  partner_name: string | null;
  awaiting: "kill" | "kill_confirm" | "save" | "check" | null;
  kill_proposal: { target_id: string; target_name: string } | null;
  check_results: CheckResult[];
  ghost: { characters: Record<string, CharacterKey> } | null;
}

export interface GameState {
  game_id: string;
  phase: PhaseKey;
  night: number;
  theme_key: string;
  join_code: string;
  players: PlayerSummary[];
  story: string[];
  votes: Record<string, string>;
  majority_needed: number;
  winner: "" | "citizens" | "killers" | "aborted";
  you: YouState;
}

export interface MafiaSession {
  gameId: string;
  playerId: string;
  token: string;
}

export interface ActiveGame {
  game_id: string;
  join_code: string;
  host_name: string;
  phase: PhaseKey;
  player_count: number;
}
