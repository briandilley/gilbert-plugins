import { useState } from "react";
import type { ReactElement } from "react";
import { DoorOpenIcon, SparklesIcon, UsersIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardEyebrow, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import type { ActiveGame } from "../types";

/** Theme presets offered when creating a game — keys mirror ``THEME_PRESETS``
 *  in the backend's ``game.py``; labels here are UI-only shorthand. */
const THEME_OPTIONS: { key: string; label: string }[] = [
  { key: "camping", label: "Camping trip" },
  { key: "mansion", label: "Haunted mansion" },
  { key: "cruise", label: "1920s cruise ship" },
  { key: "space", label: "Deep-space station" },
  { key: "western", label: "Old West town" },
];

interface JoinGateProps {
  activeGames: ActiveGame[];
  canCreate: boolean;
  onJoin: (code: string, name: string) => void;
  onCreate: (themeKey: string, themeText: string) => void;
  error: string | null;
}

/** Entry screen: join an existing game by code + name, or (signed-in
 *  household members only) start a new one by picking a theme. */
export function JoinGate({ activeGames, canCreate, onJoin, onCreate, error }: JoinGateProps): ReactElement {
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [themeKey, setThemeKey] = useState("surprise");
  const [themeText, setThemeText] = useState("");

  const canJoin = code.trim().length > 0 && name.trim().length > 0;
  const canSubmitCreate = themeKey !== "custom" || themeText.trim().length > 0;

  return (
    <div className="mx-auto flex max-w-md flex-col gap-4 p-4">
      <header className="flex items-center gap-2">
        <DoorOpenIcon className="h-5 w-5 text-(--signal)" />
        <h1 className="text-xl font-semibold leading-tight">Mafia</h1>
      </header>

      {error && <p className="text-sm text-destructive">{error}</p>}

      <Card>
        <CardHeader>
          <CardEyebrow>Join</CardEyebrow>
          <CardTitle>Enter a game</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="join-code">Join code</Label>
            <Input
              id="join-code"
              mono
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
              placeholder="ABC123"
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="join-name">Your name</Label>
            <Input id="join-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Name" />
          </div>
          <Button className="mt-1" disabled={!canJoin} onClick={() => onJoin(code.trim(), name.trim())}>
            Join
          </Button>

          {activeGames.length > 0 && (
            <div className="mt-2 flex flex-col gap-1.5">
              <span className="text-2xs font-mono uppercase tracking-[0.08em] text-muted-foreground">
                Open lobbies
              </span>
              <div className="flex flex-wrap gap-1.5">
                {activeGames.map((g) => (
                  <button
                    key={g.game_id}
                    type="button"
                    onClick={() => setCode(g.join_code)}
                    title={`${g.host_name}'s game — ${g.player_count} players`}
                    className="rounded-md border border-border px-2 py-1 font-mono text-xs text-foreground/85 transition-colors hover:border-border-strong hover:bg-foreground/5"
                  >
                    {g.join_code}
                  </button>
                ))}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {canCreate ? (
        <Card>
          <CardHeader>
            <CardEyebrow>Host</CardEyebrow>
            <CardTitle>New game</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            <div role="radiogroup" aria-label="Theme" className="flex flex-col gap-1.5">
              {THEME_OPTIONS.map((opt) => (
                <label key={opt.key} className="flex items-center gap-2 text-sm">
                  <input
                    type="radio"
                    name="theme"
                    checked={themeKey === opt.key}
                    onChange={() => setThemeKey(opt.key)}
                    className="accent-(--signal)"
                  />
                  {opt.label}
                </label>
              ))}
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="theme"
                  checked={themeKey === "custom"}
                  onChange={() => setThemeKey("custom")}
                  className="accent-(--signal)"
                />
                Custom
              </label>
              {themeKey === "custom" && (
                <Input
                  value={themeText}
                  onChange={(e) => setThemeText(e.target.value)}
                  placeholder="Describe the setting…"
                  className="ml-6"
                />
              )}
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="theme"
                  checked={themeKey === "surprise"}
                  onChange={() => setThemeKey("surprise")}
                  className="accent-(--signal)"
                />
                <SparklesIcon className="h-3.5 w-3.5" />
                Surprise me
              </label>
            </div>
            <Button
              variant="outline"
              disabled={!canSubmitCreate}
              onClick={() => onCreate(themeKey, themeText.trim())}
            >
              <UsersIcon />
              Start a new game
            </Button>
          </CardContent>
        </Card>
      ) : (
        <p className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground">
          Ask a signed-in member of the household to create the game.
        </p>
      )}
    </div>
  );
}
