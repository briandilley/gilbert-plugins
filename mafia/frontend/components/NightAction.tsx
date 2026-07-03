import { useState } from "react";
import type { ComponentType, ReactElement } from "react";
import { CheckIcon, MoonIcon, SearchIcon, ShieldIcon, SkullIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardEyebrow, CardHeader, CardTitle } from "@/components/ui/card";

import type { GameState } from "../types";

type NightAction = "kill" | "save" | "check";

const PROMPTS: Record<
  NightAction,
  { label: string; Icon: ComponentType<{ className?: string }>; verb: string }
> = {
  kill: { label: "Choose your target", Icon: SkullIcon, verb: "Confirm kill" },
  save: { label: "Choose who to protect", Icon: ShieldIcon, verb: "Confirm protection" },
  check: { label: "Choose who to investigate", Icon: SearchIcon, verb: "Confirm investigation" },
};

interface NightActionProps {
  state: GameState;
  /** Submit this player's night action (target required for kill/save/check). */
  onSubmit: (action: "kill" | "save" | "check" | "ready", targetId?: string) => void;
  busy: boolean;
}

/** Night screen. Everyone acts at the same time — no eyes-closed. You pick a
 *  target (or just tap Next as a citizen), then a second tap submits it, so a
 *  stray touch never locks in a choice. Killers see their partner's pick and
 *  the choice locks only once the duo agrees. A "N of M ready" counter shows
 *  the table's progress. */
export function NightAction({ state, onSubmit, busy }: NightActionProps): ReactElement {
  const { you } = state;
  const [selected, setSelected] = useState<string | null>(you.your_night_pick);

  const readyLine = `${state.night_ready_count} of ${state.alive_count} ready`;

  // Submitted (or nothing left to do) → wait on the rest of the table.
  if (you.awaiting === null) {
    return (
      <Card>
        <CardContent className="flex flex-col items-center gap-2 py-8 text-center">
          <MoonIcon className="h-5 w-5 text-(--signal)" />
          <p className="text-sm text-foreground/80">
            {you.character === "killer" && you.kill_locked
              ? "Your mark is chosen. Waiting for the others…"
              : "Locked in. Waiting for the others…"}
          </p>
          <p className="text-xs text-muted-foreground">{readyLine}</p>
        </CardContent>
      </Card>
    );
  }

  // Citizen → a single Next button, nothing to choose.
  if (you.awaiting === "ready") {
    return (
      <Card>
        <CardHeader>
          <CardEyebrow>Night</CardEyebrow>
          <CardTitle>Night falls</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <p className="text-sm text-muted-foreground">
            The town sleeps while others make their move. Tap Next when you&apos;re ready to go on.
          </p>
          <Button disabled={busy} onClick={() => onSubmit("ready")}>
            <CheckIcon />
            I&apos;m ready
          </Button>
          <p className="text-center text-xs text-muted-foreground">{readyLine}</p>
        </CardContent>
      </Card>
    );
  }

  const action: NightAction = you.awaiting;
  const prompt = PROMPTS[action];
  const { Icon } = prompt;
  // Killers can't target each other (backend rejects it) — hide the partner.
  const targets = state.players.filter(
    (p) =>
      p.alive &&
      p.player_id !== you.player_id &&
      !(action === "kill" && you.partner_name !== null && p.name === you.partner_name),
  );

  return (
    <Card>
      <CardHeader>
        <CardEyebrow className="flex items-center gap-1.5">
          <Icon className="h-3 w-3" />
          Your move
        </CardEyebrow>
        <CardTitle>{prompt.label}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {action === "kill" && you.partner_name && (
          <p className="text-xs text-muted-foreground">
            {you.partner_pick
              ? `${you.partner_name} is eyeing ${you.partner_pick.target_name}. Agree on one target to strike.`
              : `Waiting for ${you.partner_name} to choose — you must pick the same target.`}
          </p>
        )}

        <div className="flex flex-col gap-1">
          {targets.map((p) => {
            const isSelected = selected === p.player_id;
            const isPartnerPick = action === "kill" && you.partner_pick?.target_id === p.player_id;
            return (
              <button
                key={p.player_id}
                type="button"
                disabled={busy}
                onClick={() => setSelected(p.player_id)}
                className={
                  "flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-left text-sm transition-colors disabled:opacity-40 " +
                  (isSelected
                    ? "border-(--signal) bg-(--signal)/10 text-foreground"
                    : "border-border hover:border-border-strong hover:bg-foreground/5")
                }
              >
                <span className="truncate">{p.name}</span>
                {isPartnerPick && (
                  <span className="rounded bg-destructive/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-destructive">
                    partner
                  </span>
                )}
              </button>
            );
          })}
        </div>

        <Button
          disabled={busy || selected === null}
          onClick={() => selected && onSubmit(action, selected)}
        >
          <CheckIcon />
          {prompt.verb}
        </Button>
        <p className="text-center text-xs text-muted-foreground">{readyLine}</p>
      </CardContent>
    </Card>
  );
}
