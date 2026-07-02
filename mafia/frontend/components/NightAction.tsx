import type { ComponentType, ReactElement } from "react";
import { MoonIcon, SearchIcon, ShieldIcon, SkullIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

import type { GameState } from "../types";

const PROMPTS: Record<"kill" | "save" | "check", { label: string; Icon: ComponentType<{ className?: string }> }> = {
  kill: { label: "Choose who to kill", Icon: SkullIcon },
  save: { label: "Choose who to save", Icon: ShieldIcon },
  check: { label: "Choose who to check", Icon: SearchIcon },
};

interface NightActionProps {
  state: GameState;
  onPick: (targetId: string) => void;
  busy: boolean;
}

/** Night-phase action screen — near-black by design: players' eyes are
 *  closed and a bright phone would give the game away. Reads
 *  ``state.you.awaiting`` to decide what to show: a target picker for
 *  ``kill``/``save``/``check``, a confirm banner for ``kill_confirm`` (only
 *  the proposed target is tappable), or an idle "eyes closed" screen. */
export function NightAction({ state, onPick, busy }: NightActionProps): ReactElement {
  const { you } = state;

  if (you.awaiting === null) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 rounded-md border border-border/40 bg-black py-16 text-center">
        <MoonIcon className="h-5 w-5 text-foreground/30" />
        <p className="text-xs text-foreground/40">Keep your eyes closed…</p>
      </div>
    );
  }

  if (you.awaiting === "kill_confirm") {
    const proposal = you.kill_proposal;
    return (
      <div className="flex flex-col gap-3 rounded-md border border-destructive/40 bg-black p-4">
        <p className="text-sm text-destructive">
          Your partner picked <span className="font-medium">{proposal?.target_name}</span> — tap them to confirm.
        </p>
        {proposal && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onPick(proposal.target_id)}
            className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-left text-sm text-foreground transition-colors hover:bg-destructive/20 disabled:opacity-40"
          >
            {proposal.target_name}
          </button>
        )}
      </div>
    );
  }

  const prompt = PROMPTS[you.awaiting];
  const { Icon } = prompt;
  const targets = state.players.filter((p) => p.alive && p.player_id !== you.player_id);

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border/40 bg-black p-4">
      <p className="flex items-center gap-1.5 text-xs font-medium text-foreground/70">
        <Icon className="h-3.5 w-3.5" />
        {prompt.label}
      </p>
      <div className="flex flex-col gap-1">
        {targets.map((p) => (
          <Button key={p.player_id} variant="outline" className="justify-start" disabled={busy} onClick={() => onPick(p.player_id)}>
            {p.name}
          </Button>
        ))}
      </div>
    </div>
  );
}
