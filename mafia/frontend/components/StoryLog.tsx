import { useEffect, useRef } from "react";
import type { ReactElement } from "react";
import { ScrollTextIcon } from "lucide-react";

interface StoryLogProps {
  story: string[];
}

/** Scrollable narration transcript — newest line last, auto-scrolls to the
 *  bottom as new beats arrive. */
export function StoryLog({ story }: StoryLogProps): ReactElement {
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [story.length]);

  return (
    <div className="flex flex-col gap-1 rounded-md border border-border bg-card p-3">
      <p className="flex items-center gap-1.5 text-2xs font-mono uppercase tracking-[0.08em] text-muted-foreground">
        <ScrollTextIcon className="h-3 w-3" />
        Story
      </p>
      <div className="max-h-48 overflow-y-auto">
        <ol className="flex flex-col gap-1.5">
          {story.map((line, i) => (
            <li key={i} className="text-xs leading-relaxed text-foreground/85">
              {line}
            </li>
          ))}
        </ol>
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
