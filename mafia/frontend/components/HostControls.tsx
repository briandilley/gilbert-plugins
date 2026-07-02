import { useState } from "react";
import type { ReactElement } from "react";
import { ChevronUpIcon, OctagonAlertIcon, SettingsIcon, SkipForwardIcon, UserMinusIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

import type { GameState } from "../types";

interface HostControlsProps {
  state: GameState;
  onSkip: () => void;
  onEndDay: () => void;
  onRemove: (playerId: string) => void;
  onAbort: () => void;
}

/** Host-only collapsed drawer at the bottom of the page: skip the current
 *  night phase, end the day vote early, remove a living player, or abort the
 *  game entirely (second tap confirms). Renders nothing for non-hosts. */
export function HostControls({ state, onSkip, onEndDay, onRemove, onAbort }: HostControlsProps): ReactElement | null {
  const [open, setOpen] = useState(false);
  const [confirmAbort, setConfirmAbort] = useState(false);
  const [removeTarget, setRemoveTarget] = useState("");

  if (!state.you.is_host) return null;

  const isNight = state.phase.startsWith("night");
  const isDay = state.phase === "day";
  const living = state.players.filter((p) => p.alive);

  return (
    <div className="rounded-md border border-border bg-card">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-xs font-medium text-muted-foreground"
      >
        <span className="flex items-center gap-1.5">
          <SettingsIcon className="h-3.5 w-3.5" />
          Host controls
        </span>
        <ChevronUpIcon className={`h-3.5 w-3.5 transition-transform ${open ? "" : "rotate-180"}`} />
      </button>

      {open && (
        <div className="flex flex-col gap-2 border-t border-border p-3">
          <div className="flex flex-wrap gap-2">
            {isNight && (
              <Button variant="outline" size="sm" onClick={onSkip}>
                <SkipForwardIcon />
                Skip phase
              </Button>
            )}
            {isDay && (
              <Button variant="outline" size="sm" onClick={onEndDay}>
                <SkipForwardIcon />
                End day
              </Button>
            )}
          </div>

          <div className="flex items-center gap-2">
            <select
              value={removeTarget}
              onChange={(e) => setRemoveTarget(e.target.value)}
              className="h-6 rounded-md border border-border bg-transparent px-2 text-xs"
            >
              <option value="">Remove player…</option>
              {living.map((p) => (
                <option key={p.player_id} value={p.player_id}>
                  {p.name}
                </option>
              ))}
            </select>
            <Button
              variant="outline"
              size="sm"
              disabled={!removeTarget}
              onClick={() => {
                onRemove(removeTarget);
                setRemoveTarget("");
              }}
            >
              <UserMinusIcon />
              Remove
            </Button>
          </div>

          <Button
            variant="destructive"
            size="sm"
            onBlur={() => setConfirmAbort(false)}
            onClick={() => {
              if (confirmAbort) {
                onAbort();
                setConfirmAbort(false);
              } else {
                setConfirmAbort(true);
              }
            }}
          >
            <OctagonAlertIcon />
            {confirmAbort ? "Tap again to abort" : "Abort game"}
          </Button>
        </div>
      )}
    </div>
  );
}
