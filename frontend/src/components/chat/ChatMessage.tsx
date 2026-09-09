"use client";

import { memo, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Copy,
  Check,
  SlidersHorizontal,
  Wrench,
  ChevronRight,
  Volume2,
  Square,
  Loader2,
  Send,
  X,
} from "lucide-react";
import type { Message } from "@/lib/types";
import { api } from "@/lib/api";
import { Markdown } from "@/components/Markdown";
import { Badge, Button } from "@/components/ui/primitives";
import { toast } from "@/components/ui/toast";
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
          : "text-black hover:bg-[var(--color-surface-2)] hover:text-black dark:text-white dark:hover:text-white",
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

// The watcher emits [[TWEET_CONFIRM:<code>]] when it stages a tweet. Matching it
// here is what turns the raw agent output into an approval card.
export const TWEET_CONFIRM_RE = /\[\[TWEET_CONFIRM:([0-9a-f]{4,8})\]\]/i;

// Belt and braces over the server-side filter in falcon/agent_redact.py, which
// is the thing that actually guarantees these never arrive. This catches the one
// case the server cannot: a message already sitting in a client cache from
// before the filter existed. Never rely on it — a client-side strip is a
// rendering convenience, not a boundary.
const AGENT_BLOCK_RE =
  /\[(?:AGENT|ACTION)\s*:\s*[^\]]+\][\s\S]*?(?:\[\/(?:AGENT|ACTION)\]|$)/gi;
const AGENT_RESULT_DELIM_RE = /\[\/?AGENT RESULT\]/gi;

function stripAgentBlocks(text: string): string {
  return text
    .replace(AGENT_BLOCK_RE, "")
    .replace(AGENT_RESULT_DELIM_RE, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

// Tool results get the delimiters removed and nothing else. A read_document
// result is a real document, and a document is allowed to contain the string
// "[AGENT: ...]" — rewriting one to be safe would corrupt the very thing the
// user asked to see. The server already decided what this field may contain,
// which is why the client does not need to second-guess the body.
function stripResultDelimiters(text: string): string {
  return text.replace(AGENT_RESULT_DELIM_RE, "").trim();
}

/** Post / Reject card for a tweet the agent has proposed.
 *
 * Status comes from the server rather than the chat message, so a tweet that was
 * already posted or rejected still renders correctly after a reload — the
 * message text is immutable history, the decision is not.
 *
 * Exported because post_tweet can also be run from the Watcher Agents tab, and a
 * tweet staged there needs the same approval step. Nothing about the card is
 * chat-specific: it takes an identity and a code and reads its state from the
 * server, so the tweet is approved wherever it was staged.
 */
export function TweetConfirmCard({ identityId, code }: { identityId: string; code: string }) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState<"post" | "reject" | null>(null);
  // null until the user types — lets the server's copy stay authoritative while
  // untouched, so a refetch doesn't clobber an edit in progress either way.
  const [draft, setDraft] = useState<string | null>(null);
  const { data: tweet, isLoading } = useQuery({
    queryKey: ["staged-tweet", identityId, code],
    queryFn: () => api.stagedTweet(identityId, code),
    enabled: !!identityId && !!code,
    staleTime: 5_000,
  });

  // The agent's original wording, captured once, so Revert restores what it
  // actually proposed rather than the most recently auto-saved edit.
  const originalRef = useRef<string | null>(null);
  useEffect(() => {
    if (tweet && originalRef.current === null) originalRef.current = tweet.text;
  }, [tweet]);

  const text = draft ?? tweet?.text ?? "";
  const limit = tweet?.max_chars ?? 280;
  const overLimit = text.length > limit;
  const dirty = draft !== null && tweet != null && draft.trim() !== tweet.text;
  const changedFromOriginal =
    originalRef.current !== null && text.trim() !== originalRef.current;

  async function decide(action: "post" | "reject") {
    setBusy(action);
    try {
      const res =
        action === "post"
          ? // Send the on-screen text with the click. Nothing has to be saved
            // first, so there is no way to post a draft you had edited away.
            await api.confirmTweet(identityId, code, text.trim())
          : await api.cancelTweet(identityId, code);
      toast.success(res.message);
      if (action === "post") setDraft(null);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
      qc.invalidateQueries({ queryKey: ["staged-tweet", identityId, code] });
    }
  }

  // Persist an edit when focus leaves the box, so it survives a reload even if
  // the user walks away without posting.
  async function saveDraft() {
    if (!dirty || overLimit || !text.trim()) return;
    try {
      await api.editTweet(identityId, code, text.trim());
      qc.invalidateQueries({ queryKey: ["staged-tweet", identityId, code] });
    } catch {
      /* Non-fatal: the text still goes out with Post. */
    }
  }

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 py-2 text-[0.78rem] text-[var(--color-fg-subtle)]">
        <Loader2 className="h-3.5 w-3.5 spin" /> Loading tweet…
      </div>
    );
  }
  if (!tweet) {
    return (
      <p className="py-2 text-[0.78rem] text-[var(--color-fg-subtle)]">
        This staged tweet is no longer available.
      </p>
    );
  }

  const posted = tweet.status === "posted";
  const url = posted ? tweet.result.replace(/^Tweet posted:\s*/, "") : "";

  return (
    <div className="space-y-2.5">
      {tweet.status === "pending" ? (
        <div className="rounded-lg border border-[var(--color-border-strong)] bg-[var(--color-bg)] focus-within:border-[var(--color-fg)]">
          <textarea
            value={text}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={saveDraft}
            disabled={busy !== null}
            rows={Math.min(8, Math.max(2, text.split("\n").length + 1))}
            aria-label="Tweet text — edit before posting"
            className="w-full resize-y bg-transparent px-3 pt-2.5 text-[0.88rem] leading-relaxed text-[var(--color-fg)] focus:outline-none"
          />
          <div className="flex items-center gap-2 px-3 pb-2">
            <span
              className={cn(
                "text-[0.68rem]",
                overLimit ? "font-medium text-[var(--color-red)]" : "text-[var(--color-fg-subtle)]",
              )}
            >
              {text.length}/{limit}
            </span>
            {changedFromOriginal && (
              <>
                <span className="text-[0.68rem] text-[var(--color-fg-subtle)]">edited</span>
                <button
                  onClick={() => setDraft(originalRef.current)}
                  className="ml-auto text-[0.68rem] text-[var(--color-fg-subtle)] underline hover:text-[var(--color-fg)]"
                >
                  Revert to original
                </button>
              </>
            )}
          </div>
        </div>
      ) : (
        <div className="rounded-lg border border-[var(--color-border-strong)] bg-[var(--color-bg)] px-3 py-2.5">
          <p className="whitespace-pre-wrap text-[0.88rem] text-[var(--color-fg)]">{tweet.text}</p>
          <p className="mt-1.5 text-[0.68rem] text-[var(--color-fg-subtle)]">
            {tweet.text.length} characters
          </p>
        </div>
      )}

      {tweet.status === "pending" ? (
        <>
          {/* A previous attempt was refused by X. Nothing was published, so the
              tweet is still here to retry once the cause is fixed. */}
          {tweet.result && (
            <p className="rounded-md border border-[var(--color-red)]/30 bg-[var(--color-red)]/5 px-2.5 py-1.5 text-[0.75rem] text-[var(--color-red)]">
              Last attempt failed — nothing was posted. {tweet.result.replace(/^\[ERROR\]\s*/, "")}
            </p>
          )}
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              onClick={() => decide("post")}
              disabled={busy !== null || overLimit || !text.trim()}
            >
              {busy === "post" ? <Loader2 className="h-3.5 w-3.5 spin" /> : <Send className="h-3.5 w-3.5" />}
              {tweet.result ? "Try again" : "Post"}
            </Button>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => decide("reject")}
              disabled={busy !== null}
            >
              {busy === "reject" ? <Loader2 className="h-3.5 w-3.5 spin" /> : <X className="h-3.5 w-3.5" />}
              Reject
            </Button>
          </div>
          <p className="text-[0.68rem] text-[var(--color-fg-subtle)]">
            Edit the text above if you want to change it. Nothing is posted until you press Post,
            which publishes exactly what is in the box — publicly, and not undoable from here.
          </p>
        </>
      ) : (
        <p className="text-[0.78rem]">
          {posted ? (
            <span className="text-[var(--color-green)]">
              Posted ·{" "}
              <a
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-[var(--color-blue)] underline"
              >
                view on X
              </a>
            </span>
          ) : tweet.status === "cancelled" ? (
            <span className="text-[var(--color-fg-subtle)]">Rejected — nothing was posted.</span>
          ) : tweet.status === "expired" ? (
            <span className="text-[var(--color-fg-subtle)]">
              Expired — ask again to post this.
            </span>
          ) : (
            <span className="text-[var(--color-red)]">{tweet.result || "Failed to post."}</span>
          )}
        </p>
      )}
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => {
        const doCopy = () => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1100);
        };
        if (navigator.clipboard?.writeText) {
          navigator.clipboard.writeText(text).then(doCopy).catch(() => {
            // clipboard API failed — fall back
            fallbackCopy(text);
            doCopy();
          });
        } else {
          fallbackCopy(text);
          doCopy();
        }
      }}
      className="rounded-md p-1 text-black hover:bg-[var(--color-surface-2)] hover:text-black dark:text-white dark:hover:text-white"
      title="Copy"
    >
      {copied ? <Check className="h-3.5 w-3.5 text-[var(--color-green)]" /> : <Copy className="h-3.5 w-3.5" />}
    </button>
  );
}

function fallbackCopy(text: string) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand("copy"); } catch { /* silent */ }
  document.body.removeChild(ta);
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
  identityId = "",
}: {
  message: Message;
  /** Timestamp of the turn's context trace, or null if none. */
  contextTs?: string | null;
  /** Stable callback — kept referentially stable by the parent so memo holds. */
  onOpenContext?: (ts: string) => void;
  /** Whether the ElevenLabs voice feature is configured/enabled. */
  canSpeak?: boolean;
  /** Needed to resolve and act on a staged tweet — the approval endpoints are
   *  identity-scoped, so the card cannot render without it. */
  identityId?: string;
}) {
  const isUser = message.role === "user";
  const isWatcher = !!message._watcher;
  const hasContext = !!contextTs;
  const ttsId = message.timestamp || "";
  const canSpeakThis = canSpeak && !!ttsId && !message._suppressed && !!message.content;

  if (isUser) {
    return (
      // data-msg-ts lets ChatTab anchor the viewport to a specific row, so a
      // bulk change above the reader (the retention trim) can be compensated.
      <div data-msg-ts={message.timestamp || undefined} className="flex justify-end px-4 py-1.5">
        <div className="max-w-[85%] rounded-2xl bg-[var(--color-user-bubble)] px-4 py-2.5">
          <Markdown>{message.content}</Markdown>
        </div>
      </div>
    );
  }

  // ── Watcher result ─────────────────────────────────────────────────────
  // The server has already reduced this to what a reader is shown: a proof
  // line, a failure sentence, a staged tweet, or — for read_document alone — a
  // whole document, which is delivered here instead of into the model's payload
  // because it can be larger than the context window. A result with nothing to
  // show never reaches the client at all, so an empty body here means a stale
  // cache and renders as nothing rather than as an empty card.
  if (isWatcher) {
    const inner = stripResultDelimiters(message.content);
    if (!inner) return null;

    // A staged tweet renders as an approval card. The marker is stripped either
    // way so it never reaches the reader.
    const staged = inner.match(TWEET_CONFIRM_RE);
    const body = staged ? inner.replace(TWEET_CONFIRM_RE, "").trim() : inner;

    if (staged) {
      return (
        <div data-msg-ts={message.timestamp || undefined} className="px-4 py-1.5">
          <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] overflow-hidden">
            <div className="flex items-center gap-2 border-b border-[var(--color-border)] bg-[var(--color-surface-2)] px-3 py-1.5">
              <span className="text-[0.7rem] font-semibold uppercase tracking-wider text-[var(--color-fg-subtle)]">
                Tweet — your approval needed
              </span>
              <CopyButton text={body} />
            </div>
            <div className="px-3 py-2.5">
              {identityId ? (
                <TweetConfirmCard identityId={identityId} code={staged[1].toLowerCase()} />
              ) : (
                <Markdown>{body}</Markdown>
              )}
            </div>
          </div>
        </div>
      );
    }

    // A delivered document. The server only ever sends one line for a receipt —
    // a proof line or a failure sentence — so anything multi-line is content the
    // user is meant to read, and it gets the card, a scroll bound, and real
    // markdown so the download link works.
    if (inner.includes("\n")) {
      return (
        <div data-msg-ts={message.timestamp || undefined} className="px-4 py-1.5">
          <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] overflow-hidden">
            <div className="flex items-center gap-2 border-b border-[var(--color-border)] bg-[var(--color-surface-2)] px-3 py-1.5">
              <span className="text-[0.7rem] font-semibold uppercase tracking-wider text-[var(--color-fg-subtle)]">
                📄 Document
              </span>
              <CopyButton text={inner} />
            </div>
            {/* A full document can be very long — bounded here so one of them
                cannot bury the rest of the conversation. */}
            <div className="max-h-[32rem] overflow-y-auto px-3 py-2.5">
              <Markdown>{inner}</Markdown>
            </div>
          </div>
        </div>
      );
    }

    // One line. A bordered card with a header would be more chrome than the
    // line it wraps, and the point of the filter is that there is nothing here
    // worth framing — just the receipt.
    return (
      <div data-msg-ts={message.timestamp || undefined} className="px-4 py-1">
        <div className="flex items-start gap-2 pl-9 text-[0.78rem] text-[var(--color-fg-muted)]">
          <span className="select-none text-[var(--color-fg-subtle)]">⚙</span>
          <span className="min-w-0 flex-1 break-words font-mono">{inner}</span>
          <CopyButton text={inner} />
        </div>
      </div>
    );
  }

  return (
    <div data-msg-ts={message.timestamp || undefined} className="group px-4 py-1.5">
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
            <Markdown>
              {stripAgentBlocks(message.content) || (message._streaming ? "" : "[no output]")}
            </Markdown>
          )}
          {message._streaming && !message.content && (
            <span className="streaming-caret text-[var(--color-fg-subtle)]" />
          )}

          {!message._streaming && (
            <div className="mt-1 flex items-center gap-1">
              <CopyButton text={message.content} />
              {canSpeakThis && <SpeakButton id={ttsId} text={message.content} />}
              {hasContext && onOpenContext && (
                <button
                  onClick={() => onOpenContext(contextTs!)}
                  className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[0.72rem] text-black hover:bg-[var(--color-surface-2)] hover:text-black dark:text-white dark:hover:text-white"
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
