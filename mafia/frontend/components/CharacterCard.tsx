import type { ComponentType, ReactElement } from "react";
import { SearchIcon, SkullIcon, StethoscopeIcon, UserIcon } from "lucide-react";

import { Card, CardContent, CardEyebrow, CardHeader, CardTitle } from "@/components/ui/card";

import type { CharacterKey, YouState } from "../types";

const CHARACTER_META: Record<CharacterKey, { label: string; Icon: ComponentType<{ className?: string }>; className: string }> = {
  citizen: { label: "Citizen", Icon: UserIcon, className: "text-foreground/85" },
  killer: { label: "Killer", Icon: SkullIcon, className: "text-destructive" },
  doctor: { label: "Doctor", Icon: StethoscopeIcon, className: "text-success" },
  detective: { label: "Detective", Icon: SearchIcon, className: "text-info" },
};

interface CharacterCardProps {
  you: YouState;
}

/** Your character card: role + one-line duty. Killers see their partner's
 *  name (once assigned); the detective sees a running list of past checks.
 *  Renders nothing until characters are assigned (``you.character === null``). */
export function CharacterCard({ you }: CharacterCardProps): ReactElement | null {
  if (you.character === null) return null;
  const meta = CHARACTER_META[you.character];
  const { Icon } = meta;

  return (
    <Card>
      <CardHeader>
        <CardEyebrow>You are</CardEyebrow>
        <CardTitle className={`flex items-center gap-2 ${meta.className}`}>
          <Icon className="h-4 w-4" />
          {meta.label}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-xs text-muted-foreground">{dutyLine(you)}</p>
      </CardContent>
    </Card>
  );
}

function dutyLine(you: YouState): string {
  switch (you.character) {
    case "killer":
      return you.partner_name
        ? `Work with ${you.partner_name} to eliminate the town, unnoticed.`
        : "Eliminate the town, unnoticed. You have no living partner.";
    case "doctor":
      return "Each night, choose one player to save from the kill.";
    case "detective":
      return you.check_results.length > 0
        ? `You've checked: ${you.check_results.map((c) => `${c.name} (${c.is_killer ? "killer" : "not a killer"})`).join(", ")}.`
        : "Each night, check one player to learn if they're a killer.";
    case "citizen":
      return "Survive the night. Find the killers during the day.";
    default:
      return "";
  }
}
