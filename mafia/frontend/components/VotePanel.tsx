import type { ReactElement } from "react";
import { GavelIcon } from "lucide-react";

import type { GameState, PlayerSummary } from "../types";

interface VotePanelProps {
  state: GameState;
  onVote: (target: string | null) => void;
  busy: boolean;
}

/** Day-phase open vote: a live tally row per living player (bar + voter
 *  names), an Abstain row, and a majority-needed header. Tapping a row casts
 *  your vote for it; tapping your current pick again clears it (sends
 *  ``null``). */
export function VotePanel({ state, onVote, busy }: VotePanelProps): ReactElement {
  const { you, players } = state;
  const playerName = (id: string): string => players.find((p) => p.player_id === id)?.name ?? id;

  const votersFor = (targetId: string): string[] =>
    Object.entries(state.votes)
      .filter(([, target]) => target === targetId)
      .map(([voter]) => playerName(voter));

  const myVote = state.votes[you.player_id] ?? null;
  const living = players.filter((p) => p.alive);
  const maxVotes = Math.max(1, ...living.map((p) => votersFor(p.player_id).length));

  const castOrClear = (target: string): void => onVote(myVote === target ? null : target);

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border bg-card p-3">
      <p className="flex items-center gap-1.5 text-xs font-medium text-foreground/70">
        <GavelIcon className="h-3.5 w-3.5" />
        Vote — majority = {state.majority_needed}
      </p>

      <ul className="flex flex-col gap-1">
        {living.map((p) => (
          <VoteRow
            key={p.player_id}
            player={p}
            voters={votersFor(p.player_id)}
            maxVotes={maxVotes}
            selected={myVote === p.player_id}
            busy={busy}
            onTap={() => castOrClear(p.player_id)}
          />
        ))}
        <li>
          <button
            type="button"
            disabled={busy}
            onClick={() => castOrClear("abstain")}
            className={`flex w-full items-center justify-between gap-2 rounded-md border px-2.5 py-1.5 text-sm transition-colors disabled:opacity-40 ${
              myVote === "abstain"
                ? "border-(--signal)/50 bg-(--signal)/10 text-(--signal)"
                : "border-border hover:bg-foreground/5"
            }`}
          >
            <span>Abstain</span>
            <span className="font-mono text-xs text-muted-foreground">{votersFor("abstain").length}</span>
          </button>
        </li>
      </ul>
    </div>
  );
}

interface VoteRowProps {
  player: PlayerSummary;
  voters: string[];
  maxVotes: number;
  selected: boolean;
  busy: boolean;
  onTap: () => void;
}

/** One tally row: name + count bar + voter names, tap to (un)vote. */
function VoteRow({ player, voters, maxVotes, selected, busy, onTap }: VoteRowProps): ReactElement {
  const count = voters.length;
  const pct = Math.round((count / maxVotes) * 100);

  return (
    <li>
      <button
        type="button"
        disabled={busy}
        onClick={onTap}
        className={`relative w-full overflow-hidden rounded-md border px-2.5 py-1.5 text-left transition-colors disabled:opacity-40 ${
          selected ? "border-(--signal)/50 bg-(--signal)/10" : "border-border hover:bg-foreground/5"
        }`}
      >
        {count > 0 && (
          <span aria-hidden className="absolute inset-y-0 left-0 bg-foreground/8" style={{ width: `${pct}%` }} />
        )}
        <span className="relative flex items-center justify-between gap-2">
          <span className={`text-sm ${selected ? "text-(--signal)" : ""}`}>{player.name}</span>
          <span className="font-mono text-xs text-muted-foreground">{count}</span>
        </span>
        {voters.length > 0 && (
          <span className="relative mt-0.5 block truncate text-2xs text-muted-foreground">{voters.join(", ")}</span>
        )}
      </button>
    </li>
  );
}
