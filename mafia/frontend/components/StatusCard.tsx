import type { ReactElement } from "react";
import { Loader2Icon } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";

interface StatusCardProps {
  text: string;
}

/** A transient "Gilbert is working" card shown between phases while the
 *  narration runs — so a slow AI/TTS round-trip reads as "calculating the
 *  night…" rather than a frozen screen. Replaces the action panel until the
 *  next state push clears the status. */
export function StatusCard({ text }: StatusCardProps): ReactElement {
  return (
    <Card>
      <CardContent className="flex flex-col items-center gap-3 py-10 text-center">
        <Loader2Icon className="h-6 w-6 animate-spin text-(--signal)" />
        <p className="text-sm font-medium text-foreground/80">{text}</p>
        <p className="text-xs text-muted-foreground">Gilbert is narrating…</p>
      </CardContent>
    </Card>
  );
}
