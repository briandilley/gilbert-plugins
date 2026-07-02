import type { ReactElement } from "react";
import { GhostIcon } from "lucide-react";

import type { CharacterKey, GameState } from "../types";

import { StoryLog } from "./StoryLog";

const CHARACTER_LABEL: Record<CharacterKey, string> = {
  citizen: "Citizen",
  killer: "Killer",
  doctor: "Doctor",
  detective: "Detective",
};

interface GhostPanelProps {
  state: GameState;
}

/** Ghost view: shown once you're dead or the game has ended. Reveals every
 *  player's character and keeps the story log visible so you can watch the
 *  rest play out. */
export function GhostPanel({ state }: GhostPanelProps): ReactElement {
  const characters = state.you.ghost?.characters ?? {};
  const playerName = (id: string): string => state.players.find((p) => p.player_id === id)?.name ?? id;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2 rounded-md border border-border bg-card px-3 py-2">
        <GhostIcon className="h-4 w-4 text-muted-foreground" />
        <span className="text-sm text-muted-foreground">
          {state.winner ? `Game over — ${state.winner} win.` : "You are dead — enjoy the show."}
        </span>
      </div>

      <ul className="divide-y divide-border rounded-md border border-border">
        {Object.entries(characters).map(([playerId, character]) => (
          <li key={playerId} className="flex items-center justify-between gap-2 px-3 py-2 text-sm">
            <span className="truncate">{playerName(playerId)}</span>
            <span className="font-mono text-xs text-muted-foreground">{CHARACTER_LABEL[character]}</span>
          </li>
        ))}
      </ul>

      <StoryLog story={state.story} />
    </div>
  );
}
