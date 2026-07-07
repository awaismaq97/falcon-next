"use client";

import { memo, useState } from "react";
import { Copy, Check, SlidersHorizontal, Wrench, ChevronRight, Volume2, Square, Loader2 } from "lucide-react";
import type { Message } from "@/lib/types";
import { Markdown } from "@/components/Markdown";
import { Badge } from "@/components/ui/primitives";
import { useTts } from "@/lib/tts";
import { cn } from "@/lib/utils";

// "Read aloud" toggle. Subscribes to the global TTS store with selectors keyed
// to this message's id, so only this button re-renders as playback state moves.
function SpeakButton({ id, text }: { id: string; text: string }) {
  const playing = useTts((s) => s.playingId === id);
  const loading = useTts((s) => s.loadingId === id);
  const toggle = useTts((s) => s.toggle);
  return (
    <button
      onClick={() => toggle(id, text)}
      className={cn(
        "inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[0.72rem] transition-colors",
        playing
          ? "text-[var(--color-accent)]"
          : "text-[var(--color-fg-subtle)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-fg)]",
      )}
      title={playing ? "Stop" : loading ? "Synthesising…" : "Read aloud"}
    >
      {loading ? (
        <Loader2 className="h-3.5 w-3.5 spin" />
      ) : playing ? (
        <Square className="h-3 w-3 fill-current" />
      ) : (
        <Volume2 className="h-3.5 w-3.5" />
      )}
    </button>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1100);
        });
      }}
      className="rounded-md p-1 text-[var(--color-fg-subtle)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-fg)]"
      title="Copy"
    >
      {copied ? <Check className="h-3.5 w-3.5 text-[var(--color-green)]" /> : <Copy className="h-3.5 w-3.5" />}
    </button>
  );
}

function ToolEvents({ events }: { events: NonNullable<Message["_events"]> }) {
  const [open, setOpen] = useState(false);
  if (!events.length) return null;
  const calls = events.filter((e) => e.type === "tool_call");
  return (
    <div className="mb-2">
      <button
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center gap-1.5 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 text-[0.72rem] text-[var(--color-fg-muted)] hover:text-[var(--color-fg)]"
      >
        <Wrench className="h-3 w-3" />
        {calls.length} tool call{calls.length !== 1 ? "s" : ""}
        <ChevronRight className={cn("h-3 w-3 transition-transform", open && "rotate-90")} />
      </button>
      {open && (
        <div className="mt-1.5 space-y-1.5">
          {events.map((e, i) => (
            <div key={i} className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-2 text-[0.72rem]">
              <div className="flex items-center gap-1.5">
                <Badge color={e.type === "tool_call" ? "blue" : "green"}>{e.type}</Badge>
                <span className="font-mono text-[var(--color-fg)]">{e.tool}</span>
              </div>
              <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-[0.68rem] text-[var(--color-fg-muted)]">
                {e.type === "tool_call" ? JSON.stringify(e.args) : e.content}
              </pre>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export const ChatMessage = memo(function ChatMessage({
  message,
  contextTs,
  onOpenContext,
  canSpeak = false,
}: {
  message: Message;
  /** Timestamp of the turn's context trace, or null if none. */
  contextTs?: string | null;
  /** Stable callback — kept referentially stable by the parent so memo holds. */
  onOpenContext?: (ts: string) => void;
  /** Whether the ElevenLabs voice feature is configured/enabled. */
  canSpeak?: boolean;
}) {
  const isUser = message.role === "user";
  const hasContext = !!contextTs;
  // A persisted assistant message with real text can be read aloud. Keyed on its
  // timestamp so the button reflects that exact message's playback state.
  const ttsId = message.timestamp || "";
  const canSpeakThis = canSpeak && !!ttsId && !message._suppressed && !!message.content;
  const ttsActive = useTts((s) => !!ttsId && (s.playingId === ttsId || s.loadingId === ttsId));

  if (isUser) {
    return (
      <div className="flex justify-end px-4 py-1.5">
        <div className="max-w-[85%] rounded-2xl bg-[var(--color-user-bubble)] px-4 py-2.5">
          <Markdown>{message.content}</Markdown>
        </div>
      </div>
    );
  }

  return (
    <div className="group px-4 py-1.5">
      <div className="flex gap-3">
        <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-[var(--color-accent)] text-[0.7rem] text-[var(--color-bg)]">
          🦅
        </div>
        <div className="min-w-0 flex-1">
          {message._events && <ToolEvents events={message._events} />}
          {message._warning && (
            <div className="mb-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-[0.78rem] text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
              ⚠️ {message._warning}
            </div>
          )}
          {message._suppressed ? (
            <div className="italic text-[var(--color-fg-subtle)]">[suppressed]</div>
          ) : (
            <Markdown>{message.content || (message._streaming ? "" : "[no output]")}</Markdown>
          )}
          {message._streaming && !message.content && (
            <span className="streaming-caret text-[var(--color-fg-subtle)]" />
          )}

          {!message._streaming && (
            <div
              className={cn(
                "mt-1 flex items-center gap-1 transition-opacity",
                ttsActive ? "opacity-100" : "opacity-0 group-hover:opacity-100",
              )}
            >
              <CopyButton text={message.content} />
              {canSpeakThis && <SpeakButton id={ttsId} text={message.content} />}
              {hasContext && onOpenContext && (
                <button
                  onClick={() => onOpenContext(contextTs!)}
                  className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[0.72rem] text-[var(--color-fg-subtle)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-fg)]"
                  title="View the exact context sent for this turn"
                >
                  <SlidersHorizontal className="h-3 w-3" /> context
                </button>
              )}
              {message._judge && (
                <Badge color={message._judge.verdict === "pass" ? "green" : "red"}>
                  judge: {message._judge.verdict}
                </Badge>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
});
