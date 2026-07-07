"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown } from "lucide-react";
import { api } from "@/lib/api";
import { streamChat } from "@/lib/sse";
import {
  useHistory,
  useTraceIndex,
  useIdentityInvalidator,
  useHistoryAppender,
  useVoiceConfig,
} from "@/lib/queries";
import { useSettings } from "@/lib/store";
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
  const invalidate = useIdentityInvalidator();
  const appendHistory = useHistoryAppender();
  const canSpeak = !!voiceCfg?.enabled;

  const [pending, setPending] = useState<Message[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [visible, setVisible] = useState(PAGE);
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
  const [showJump, setShowJump] = useState(false);

  const history = useMemo(() => historyData?.messages ?? [], [historyData]);
  const traceSet = useMemo(() => new Set(traceIdx?.timestamps ?? []), [traceIdx]);

  // Combined view: persisted history + the in-flight turn.
  const allMessages = useMemo(() => [...history, ...pending], [history, pending]);
  const shown = allMessages.slice(Math.max(0, allMessages.length - visible));
  const hiddenCount = allMessages.length - shown.length;

  const openContext = useCallback((ts: string) => setContextTs(ts), []);

  const scrollToBottom = useCallback((smooth = false) => {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = true;
    setShowJump(false);
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }, []);

  // Track whether the user is pinned to the bottom. If they scroll up we stop
  // auto-following and reveal the "jump to latest" button.
  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    stickRef.current = atBottom;
    setShowJump(!atBottom); // no-op re-render when value is unchanged (React bails)
  }, []);

  // Follow streaming output, but only while the user is at the bottom. Coalesced
  // into a single rAF so a burst of tokens triggers at most one layout write.
  useEffect(() => {
    if (!stickRef.current) return;
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  }, [pending, streaming]);

  // Reset paging and snap to the newest message on identity switch / first load.
  useEffect(() => {
    setVisible(PAGE);
    stickRef.current = true;
    setShowJump(false);
    requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  }, [identityId, isLoading]);

  useEffect(() => () => void (rafRef.current && cancelAnimationFrame(rafRef.current)), []);

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
    setVisible((v) => v + 2);
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
    const appended = canAppend
      ? appendHistory(identityId, [
          { role: "user", content: o!.userText, timestamp: o!.userTs },
          { role: "assistant", content: o!.asstText, timestamp: o!.asstTs },
        ])
      : false;

    invalidate(identityId, { includeHistory: !appended });

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
    const globalIdx = allMessages.length - shown.length + idx;
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
                  <Button size="sm" variant="secondary" onClick={() => setVisible((v) => v + PAGE)}>
                    Load older ({hiddenCount})
                  </Button>
                </div>
              )}
              {shown.map((m, i) => {
                const ts = m.role === "assistant" ? userTsBefore(i) : null;
                return (
                  <ChatMessage
                    key={`${m.timestamp}-${i}`}
                    message={m}
                    contextTs={ts}
                    onOpenContext={openContext}
                    canSpeak={canSpeak}
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
