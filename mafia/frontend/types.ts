/** Wire types for the mafia.* WS protocol — must mirror game.state_for(). */

export type PhaseKey = "lobby" | "night" | "day" | "ended";

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
  /** The action this player still owes for the current night, or null once
   *  they've submitted / have nothing to do. Everyone acts at once. */
  awaiting: "kill" | "save" | "check" | "ready" | null;
  /** True once this player has submitted their night action. */
  submitted: boolean;
  /** A killer's own submitted target (to highlight it in the picker). */
  your_night_pick: string | null;
  /** A killer's partner's current live pick — how the duo converges. */
  partner_pick: { target_id: string; target_name: string } | null;
  /** True once the killers agree and the kill is final. */
  kill_locked: boolean;
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
  /** Number of living players. */
  alive_count: number;
  /** How many living players have submitted their night action (NIGHT only). */
  night_ready_count: number;
  winner: "" | "citizens" | "killers" | "aborted";
  you: YouState;
}

/** The raw WS frame the server enqueues on every game-state change. */
export interface MafiaStateFrame {
  type: "mafia.state";
  game_id: string;
  state: GameState;
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

/** One selectable narration speaker in the create form's picker. Mirrors
 *  the payload built by ``MafiaService._ws_speakers_list``. */
export interface SpeakerOption {
  /** Stable id sent back on create — the speaker's display name, since
   *  the speaker service resolves announce targets by name. */
  id: string;
  name: string;
  model: string;
  backend: string;
  group_name: string;
}

export interface SpeakersResponse {
  speakers: SpeakerOption[];
  defaults: { volume: number };
}
