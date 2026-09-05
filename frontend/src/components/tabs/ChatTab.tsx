"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowDown } from "lucide-react";
import { api } from "@/lib/api";
import { streamChat } from "@/lib/sse";
import {
  useHistory,
  useTraceIndex,
  useIdentityInvalidator,
  useHistoryAppender,
  useVoiceConfig,
  useWatcherStatus,
  useWatcherResultPoller,
} from "@/lib/queries";
import { useSettings } from "@/lib/store";
import { useAuth } from "@/lib/authStore";
import { useTts } from "@/lib/tts";
import type { DocAttachment, Message, SSEEvent } from "@/lib/types";
import { ChatMessage } from "@/components/chat/ChatMessage";
import { ChatInput } from "@/components/chat/ChatInput";
import { ContextDialog } from "@/components/chat/ContextDialog";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { JsonView } from "@/components/JsonView";
import { Button, Spinner } from "@/components/ui/primitives";
import { toast } from "@/components/ui/toast";

const PAGE = 30;

// Scroll compensation must land before the browser paints, or the reader sees
// the wrong position flash first. useLayoutEffect warns during SSR, so fall
// back to useEffect on the server, where it never runs anyway.
const useIsoLayoutEffect = typeof window !== "undefined" ? useLayoutEffect : useEffect;

function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

export function ChatTab() {
  const identityId = useSettings((s) => s.identityId);
  const settings = useSettings((s) => s.settings);
  const payloadReview = useSettings((s) => s.payloadReview);
  const { data: historyData, isLoading } = useHistory(identityId);
  const { data: traceIdx } = useTraceIndex(identityId);
  const { data: voiceCfg } = useVoiceConfig();
  const { user } = useAuth();
  const invalidate = useIdentityInvalidator();
  const appendHistory = useHistoryAppender();
  // Live watcher results over SSE. The ref lets the stream know a turn is
  // in flight so a fast result doesn't render above the message it answers;
  // consumeWatcherBacklog() reports whether any were held back.
  const turnInFlight = useRef(false);
  const consumeWatcherBacklog = useWatcherResultPoller(identityId, turnInFlight);
  // canSpeak: ElevenLabs must be configured AND the user must have voice feature enabled
  const voiceFeatureEnabled = user?.role === "admin" || (user?.features?.voice !== false);
  const canSpeak = !!voiceCfg?.enabled && voiceFeatureEnabled;

  const [pending, setPending] = useState<Message[]>([]);
  const [streaming, setStreaming] = useState(false);
  // The oldest message currently on screen, identified by timestamp rather than
  // by an index or a count from the tail.
  //
  // Both of those alternatives break on this conversation. A tail-anchored
  // window ("show the last N") slides whenever a message is appended, dropping
  // the oldest row and taking its height from *above* the reader. An index or
  // count is invalidated by the retention trim in useHistoryAppender, which
  // removes messages from the front of the list wholesale. A timestamp survives
  // both: it names a row, so the boundary stays on the same message no matter
  // how the list around it changes.
  const [topTs, setTopTs] = useState<string | null>(null);
  const [contextTs, setContextTs] = useState<string | null>(null);
  const [preview, setPreview] = useState<{
    payload: unknown;
    text: string;
    dataUrls: string[];
    docs: DocAttachment[];
  } | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  // Confirmed server-side result of the in-flight turn, captured from the SSE
  // stream so we can append it to the history cache instead of refetching.
  const outcomeRef = useRef<{
    userTs: string;
    userText: string;
    asstTs: string;
    asstText: string;
    suppressed: boolean;
    done: boolean;
    error: boolean;
  } | null>(null);
  const stickRef = useRef(true); // follow new content only while pinned to bottom
  const rafRef = useRef<number | null>(null);
  const measureRef = useRef<number | null>(null);
  const [showJump, setShowJump] = useState(false);

  const history = useMemo(() => historyData?.messages ?? [], [historyData]);
  const traceSet = useMemo(() => new Set(traceIdx?.timestamps ?? []), [traceIdx]);

  // Combined view: persisted history + the in-flight turn.
  const allMessages = useMemo(() => [...history, ...pending], [history, pending]);
  // If the boundary message is gone (trimmed away, or deleted in the Logs tab)
  // there is nothing older left to hide, so show everything that remains.
  const hiddenCount = useMemo(() => {
    if (!topTs) return 0;
    const i = allMessages.findIndex((m) => m.timestamp === topTs);
    return i === -1 ? 0 : i;
  }, [allMessages, topTs]);
  const shown = allMessages.slice(hiddenCount);

  const openContext = useCallback((ts: string) => setContextTs(ts), []);

  const scrollToBottom = useCallback((smooth = false) => {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = true;
    setShowJump(false);
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }, []);

  // The rows the reader is looking at, and where in the viewport each one sits.
  // Recorded as elements, not a pixel offset: the retention trim removes whole
  // messages from the front of the list, so an offset measured before it means
  // nothing afterwards, whereas "this message was 40px below the top" still does.
  //
  // Several candidates, not one, because the trim can take the topmost visible
  // row with it. The first survivor is used, so losing the very row the reader
  // was looking at still leaves something to align against.
  const anchorRef = useRef<{ ts: string; top: number }[]>([]);

  const captureAnchor = useCallback(() => {
    const el = scrollRef.current;
    if (!el || el.clientHeight === 0) return;
    const boxTop = el.getBoundingClientRect().top;
    const found: { ts: string; top: number }[] = [];
    for (const row of Array.from(el.querySelectorAll<HTMLElement>("[data-msg-ts]"))) {
      const r = row.getBoundingClientRect();
      if (r.bottom <= boxTop) continue;        // already scrolled past
      found.push({ ts: row.dataset.msgTs!, top: r.top - boxTop });
      if (found.length === 3) break;
    }
    anchorRef.current = found;
  }, []);

  // Track whether the user is pinned to the bottom. If they scroll up we stop
  // auto-following and reveal the "jump to latest" button.
  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    stickRef.current = atBottom;
    captureAnchor();
    setShowJump(!atBottom); // no-op re-render when value is unchanged (React bails)
  }, [captureAnchor]);

  // Bring a new turn into view once, as it starts — then hold position. Chasing
  // streaming output drags text out from under the reader while they are reading
  // it, so tokens arriving is deliberately not a reason to move the viewport.
  // The dependency is the turn's existence, not `pending`, so token updates
  // (which replace `pending` on every chunk) do not re-trigger it.
  const turnStarted = pending.length > 0;
  useEffect(() => {
    if (!turnStarted || !stickRef.current) return;
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(() => {
      const el = scrollRef.current;
      // A hidden tab (display:none) reports scrollHeight 0 — writing scrollTop
      // then would land on the very top, which is the "jumped to first message"
      // bug. Skip rather than write a meaningless offset.
      if (!el || el.clientHeight === 0) return;
      el.scrollTop = el.scrollHeight;
    });
  }, [turnStarted]);

  // Content growing below the fold fires no scroll event, so `onScroll` never
  // sees the viewport drift away from the bottom while output streams in. Re-run
  // the same test after each update so "jump to latest" appears as soon as there
  // is something down there to jump to.
  //
  // Coalesce onto the next free frame rather than cancel-and-reschedule. Tokens
  // land faster than the browser paints, so cancelling on every chunk starved
  // this callback for the whole reply and left `stickRef` at whatever the turn
  // began with — `true`. A stale `true` is what let the view snap to the bottom
  // the moment a reply finished, throwing away the reader's place.
  useEffect(() => {
    if (!streaming || measureRef.current !== null) return;
    measureRef.current = requestAnimationFrame(() => {
      measureRef.current = null;
      const el = scrollRef.current;
      // Skip while the tab is hidden — a display:none container reads
      // scrollHeight/clientHeight as 0, which would falsely un-stick us.
      if (!el || el.clientHeight === 0) return;
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      stickRef.current = atBottom;
      // Capture unconditionally. Anchoring only rows the reader had scrolled
      // away from was the asymmetry behind "if I sit still it holds, if I scroll
      // along it drops me at the bottom": reading a reply as it arrives keeps
      // you inside the bottom band, so no anchor was ever recorded for the one
      // moment that needed it.
      captureAnchor();
      setShowJump(!atBottom);
    });
  }, [pending, streaming, captureAnchor]);

  // Reset paging and snap to the newest message on identity switch / first load.
  // The tab is `forceMount`ed and hidden with display:none when not active, so
  // this effect fires while the container has no layout (scrollHeight === 0);
  // writing scrollTop then would silently pin us to the top. We remember that
  // we still owe a snap-to-bottom and do it on the next resize (i.e. when the
  // tab becomes visible again).
  const pendingSnapRef = useRef(false);
  useEffect(() => {
    // Fold everything but the newest page, naming the boundary message. Set
    // once per identity/load and then held: later arrivals must not move it.
    const msgs = historyData?.messages ?? [];
    setTopTs(msgs.length > PAGE ? (msgs[msgs.length - PAGE].timestamp || null) : null);
    stickRef.current = true;
    setShowJump(false);
    pendingSnapRef.current = true;
    requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (!el || el.clientHeight === 0) return;
      el.scrollTop = el.scrollHeight;
      pendingSnapRef.current = false;
    });
    // historyData.messages.length is read but deliberately not a dependency:
    // this boundary is set once per identity/load and must then stay put.
    // Recomputing it whenever a message arrives is exactly the sliding window
    // this replaced.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identityId, isLoading]);

  // When the tab becomes visible again (display:none → laid out), pay off a
  // snap-to-bottom that could not run while the container had no height.
  //
  // Deliberately keyed on that debt alone, never on `stickRef`: this container
  // also resizes for reasons that have nothing to do with new messages — the
  // composer growing a line, its Stop button becoming Send when a reply lands, a
  // window resize — and none of those are a reason to move the reader.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const restore = () => {
      const box = scrollRef.current;
      if (!box || box.clientHeight === 0) return;
      if (!pendingSnapRef.current) return;
      box.scrollTop = box.scrollHeight;
      pendingSnapRef.current = false;
    };
    // ResizeObserver fires when the container's box goes from 0 → real (the
    // moment the tab becomes visible), and again on window resizes.
    const ro = new ResizeObserver(restore);
    ro.observe(el);
    // The browser tab coming back from background: layout may be stale.
    const onVis = () => {
      if (document.visibilityState === "visible") restore();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      ro.disconnect();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  // Hold the reader on the same message across any change to the persisted list.
  //
  // The disruptive one is the retention trim in useHistoryAppender: finishing a
  // turn replaces the cache with the newest 15 turns, which on a long
  // conversation drops a hundred-plus messages from the *front* in a single
  // commit. All that height vanishes from above the viewport, scrollHeight
  // collapses, and the browser clamps scrollTop to the new maximum — landing
  // the reader at the bottom. Re-aligning the anchor row cancels it exactly.
  //
  // Unconditional, with no "…unless they were at the bottom" escape. Realigning
  // is position-preserving by construction: everything from the anchor down is
  // put back exactly where it was, so a reader who was at the bottom stays at
  // the bottom and one who was mid-history stays mid-history. Skipping the work
  // for readers near the bottom did not keep them there — it handed them to the
  // browser's clamp, which is the jump being reported.
  //
  // Keyed on `history`, not `allMessages`: tokens arriving only extend the last
  // row downwards, which shifts nothing above the reader and so needs no
  // compensation, while running this on every chunk would race the user's own
  // scrolling (scroll events land a frame after the scrollTop they describe).
  //
  // Runs before paint, so the wrong position is never displayed.
  useIsoLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el || el.clientHeight === 0) return;
    const boxTop = el.getBoundingClientRect().top;
    for (const a of anchorRef.current) {
      const row = el.querySelector<HTMLElement>(`[data-msg-ts="${CSS.escape(a.ts)}"]`);
      if (!row) continue;                  // this one was trimmed — try the next
      const delta = row.getBoundingClientRect().top - boxTop - a.top;
      if (delta) el.scrollTop += delta;
      return;
    }
  }, [history]);

  useEffect(
    () => () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      if (measureRef.current) cancelAnimationFrame(measureRef.current);
    },
    [],
  );

  function updateAssistant(fn: (m: Message) => Message) {
    setPending((prev) => {
      if (prev.length < 2) return prev;
      const copy = [...prev];
      copy[1] = fn(copy[1]);
      return copy;
    });
  }

  function handleEvent(ev: SSEEvent) {
    switch (ev.type) {
      case "meta":
        if (outcomeRef.current) {
          outcomeRef.current.userText = ev.logged_user_input;
          outcomeRef.current.userTs = ev.user_ts;
        }
        setPending((prev) => {
          if (!prev.length) return prev;
          const copy = [...prev];
          copy[0] = { ...copy[0], content: ev.logged_user_input, timestamp: ev.user_ts };
          return copy;
        });
        break;
      case "token":
        updateAssistant((m) => ({ ...m, content: m.content + ev.text }));
        break;
      case "tool_call":
        updateAssistant((m) => ({ ...m, _events: [...(m._events ?? []), { type: "tool_call", tool: ev.tool, args: ev.args }] }));
        break;
      case "tool_result":
        updateAssistant((m) => ({ ...m, _events: [...(m._events ?? []), { type: "tool_result", tool: ev.tool, content: ev.content }] }));
        break;
      case "message":
        updateAssistant((m) => ({ ...m, content: ev.text }));
        break;
      case "warning":
        updateAssistant((m) => ({ ...m, _warning: ev.message }));
        break;
      case "done":
        if (outcomeRef.current) {
          outcomeRef.current.asstText = ev.response_text;
          outcomeRef.current.asstTs = ev.asst_ts;
          outcomeRef.current.suppressed = !!ev.suppressed;
          outcomeRef.current.done = true;
        }
        updateAssistant((m) => ({
          ...m,
          content: ev.response_text,
          timestamp: ev.asst_ts,
          _streaming: false,
          _suppressed: ev.suppressed,
          _judge: ev.judge,
        }));
        break;
      case "error":
        if (outcomeRef.current) outcomeRef.current.error = true;
        updateAssistant((m) => ({ ...m, content: `⚠️ ${ev.message}`, _streaming: false }));
        break;
    }
  }

  async function runSend(text: string, dataUrls: string[], docs: DocAttachment[]) {
    // Optimistic marker mirrors what the backend logs (images + document names),
    // so the user bubble looks right before the meta event confirms it.
    const markers: string[] = [];
    if (dataUrls.length) markers.push(`🖼 _${dataUrls.length} image${dataUrls.length !== 1 ? "s" : ""} attached_`);
    if (docs.length) markers.push(`📎 _${docs.map((d) => d.filename).join(", ")}_`);
    const marker = markers.join("\n\n");
    const userMarker = marker ? (text ? `${text}\n\n${marker}` : marker) : text;
    setPending([
      { role: "user", content: userMarker, timestamp: "" },
      { role: "assistant", content: "", timestamp: "", _streaming: true, _events: undefined },
    ]);
    setStreaming(true);
    turnInFlight.current = true;
    stickRef.current = true; // a new turn always follows to the bottom
    setShowJump(false);
    outcomeRef.current = {
      userTs: "",
      userText: "",
      asstTs: "",
      asstText: "",
      suppressed: false,
      done: false,
      error: false,
    };

    const ac = new AbortController();
    abortRef.current = ac;
    try {
      await streamChat(
        { identity_id: identityId, message: text, images: dataUrls, documents: docs, settings },
        handleEvent,
        ac.signal,
      );
    } catch {
      /* aborted */
    }

    // Happy path: append the two confirmed messages to the history cache instead
    // of refetching the whole tail. Anything unusual (suppressed / error / aborted
    // / missing timestamps) falls back to a full refetch so the UI can't drift.
    const o = outcomeRef.current;
    const canAppend =
      !!o && o.done && !o.error && !o.suppressed && !!o.userTs && !!o.asstTs;

    // Re-read the reader's position from the live DOM immediately before the
    // cache is rewritten. The streaming measurement runs on an animation frame,
    // so the last one can predate the final tokens; this is the only reading
    // guaranteed to describe what is actually on screen when the trim lands.
    captureAnchor();
    const appended = canAppend
      ? appendHistory(identityId, [
          { role: "user", content: o!.userText, timestamp: o!.userTs },
          { role: "assistant", content: o!.asstText, timestamp: o!.asstTs },
        ])
      : false;

    // Reopen the direct-append path first: the turn is already in the history
    // cache above, so anything arriving from here on lands in the right place.
    turnInFlight.current = false;
    // Results that landed mid-turn were skipped to protect ordering; refetch so
    // the server's insertion order decides where they sit.
    const missedWatcherResults = consumeWatcherBacklog();

    invalidate(identityId, { includeHistory: !appended || missedWatcherResults });

    // Auto-speak the finished response when the user has that turned on and voice
    // is configured. Reads live settings so toggling mid-stream is respected.
    if (canAppend && o && canSpeak) {
      const vp = useSettings.getState().voice;
      if (vp.auto_play && vp.voice_id) useTts.getState().play(o.asstTs, o.asstText);
    }

    setPending([]);
    setStreaming(false);
    abortRef.current = null;
    outcomeRef.current = null;
  }

  async function onSend(text: string, images: File[], docs: DocAttachment[]) {
    const dataUrls = await Promise.all(images.map(fileToDataUrl));
    if (payloadReview) {
      setPreviewLoading(true);
      try {
        const p = await api.preview(identityId, text, settings);
        setPreview({ payload: p.raw_payload, text, dataUrls, docs });
      } catch (e) {
        toast.error((e as Error).message);
      } finally {
        setPreviewLoading(false);
      }
      return;
    }
    runSend(text, dataUrls, docs);
  }

  async function confirmPreview() {
    if (!preview) return;
    const { text, dataUrls, docs } = preview;
    setPreview(null);
    runSend(text, dataUrls, docs);
  }

  function stop() {
    abortRef.current?.abort();
  }

  // Map assistant message → preceding user timestamp for the context button.
  function userTsBefore(idx: number): string | null {
    const globalIdx = hiddenCount + idx;
    const prev = allMessages[globalIdx - 1];
    if (prev?.role === "user" && prev.timestamp && traceSet.has(prev.timestamp)) return prev.timestamp;
    return null;
  }

  return (
    <div className="relative flex h-full flex-col">
      <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto py-3">
        <div className="mx-auto max-w-3xl">
          {isLoading ? (
            <div className="flex items-center justify-center gap-2 py-20 text-[var(--color-fg-subtle)]">
              <Spinner /> Loading conversation…
            </div>
          ) : allMessages.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-24 text-center">
              <div className="mb-3 text-4xl">🦅</div>
              <div className="text-lg font-semibold text-[var(--color-fg)]">Falcon</div>
              <div className="mt-1 max-w-sm text-[0.85rem] text-[var(--color-fg-subtle)]">
                A transparent inference layer. Send a message — every component entering generation is visible in the
                Context tab.
              </div>
            </div>
          ) : (
            <>
              {hiddenCount > 0 && (
                <div className="flex justify-center py-2">
                  <Button size="sm" variant="secondary" onClick={() => setTopTs(allMessages[Math.max(0, hiddenCount - PAGE)]?.timestamp ?? null)}>
                    Load older ({hiddenCount})
                  </Button>
                </div>
              )}
              {shown.map((m, i) => {
                const ts = m.role === "assistant" ? userTsBefore(i) : null;
                // Key by timestamp+position. By the time a turn is finalised the
                // live rows already carry the server's timestamps, so they key
                // identically to the persisted rows that replace them — React
                // reuses the components instead of remounting them, and the
                // reader's position survives the swap.
                return (
                  <ChatMessage
                    key={`${m.timestamp}-${i}`}
                    message={m}
                    contextTs={ts}
                    onOpenContext={openContext}
                    canSpeak={canSpeak}
                    identityId={identityId}
                  />
                );
              })}
            </>
          )}
        </div>
      </div>

      {showJump && (
        <button
          onClick={() => scrollToBottom(true)}
          className="absolute bottom-28 left-1/2 z-10 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-3 py-1.5 text-[0.75rem] text-[var(--color-fg-muted)] shadow-md transition-colors hover:text-[var(--color-fg)]"
        >
          <ArrowDown className="h-3.5 w-3.5" /> Jump to latest
        </button>
      )}

      <ChatInput onSend={onSend} onStop={stop} streaming={streaming || previewLoading} />

      {contextTs && (
        <ContextDialog
          identityId={identityId}
          userTs={contextTs}
          open={!!contextTs}
          onOpenChange={(v) => !v && setContextTs(null)}
        />
      )}

      <Dialog open={!!preview} onOpenChange={(v) => !v && setPreview(null)}>
        <DialogContent title="Review payload before sending">
          <p className="mb-3 text-[0.8rem] text-[var(--color-fg-muted)]">
            This is the exact assembled context that will be sent to the model.
          </p>
          {preview && <JsonView data={preview.payload} maxHeight="380px" />}
          <div className="mt-4 flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setPreview(null)}>
              Cancel
            </Button>
            <Button variant="primary" onClick={confirmPreview}>
              Send
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
