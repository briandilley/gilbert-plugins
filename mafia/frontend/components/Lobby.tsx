import type { ReactElement } from "react";
import { CrownIcon, UsersIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardEyebrow, CardHeader, CardTitle } from "@/components/ui/card";

import type { GameState } from "../types";

/** Minimum players required to start — mirrors ``MIN_PLAYERS`` in the
 *  backend's ``game.py``. */
const MIN_PLAYERS = 4;

interface LobbyProps {
  state: GameState;
  onStart: () => void;
}

/** Pre-game lobby: the join code players read off the host's screen, the
 *  roster as it fills in, and a host-only Start control. */
export function Lobby({ state, onStart }: LobbyProps): ReactElement {
  const count = state.players.length;
  const canStart = count >= MIN_PLAYERS;

  return (
    <Card>
      <CardHeader>
        <CardEyebrow>Lobby</CardEyebrow>
        <CardTitle>Waiting for players</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-col items-center gap-1 rounded-md border border-border bg-foreground/[0.03] py-4">
          <span className="text-2xs font-mono uppercase tracking-[0.08em] text-muted-foreground">Join code</span>
          <span className="font-mono text-3xl font-semibold tracking-[0.2em] text-(--signal)">
            {state.join_code}
          </span>
        </div>

        <ul className="divide-y divide-border rounded-md border border-border">
          {state.players.map((p) => (
            <li key={p.player_id} className="flex items-center justify-between gap-2 px-3 py-2 text-sm">
              <span className="truncate">{p.name}</span>
              {p.is_host && (
                <Badge variant="active">
                  <CrownIcon className="h-2.5 w-2.5" />
                  Host
                </Badge>
              )}
            </li>
          ))}
        </ul>

        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <UsersIcon className="h-3.5 w-3.5" />
          {count} joined — needs at least {MIN_PLAYERS} to start
        </p>

        {state.you.is_host && (
          <Button onClick={onStart} disabled={!canStart} title={canStart ? undefined : `Need at least ${MIN_PLAYERS} players`}>
            Start game
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
