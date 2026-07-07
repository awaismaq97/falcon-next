"use client";

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Download, Trash2, ScrollText } from "lucide-react";
import { api } from "@/lib/api";
import { useHistory, useTraceIndex } from "@/lib/queries";
import { useSettings } from "@/lib/store";
import type { Message, TraceStep } from "@/lib/types";
import { Button, Textarea, Badge, Spinner } from "@/components/ui/primitives";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { JsonView } from "@/components/JsonView";
import { cn, downloadJSON, fmtTime } from "@/lib/utils";
import { toast } from "@/components/ui/toast";

function TraceDialog({ identityId, ts, open, onOpenChange }: { identityId: string; ts: string; open: boolean; onOpenChange: (v: boolean) => void }) {
  const [steps, setSteps] = useState<TraceStep[] | null>(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (open && ts) {
      setLoading(true);
      api.getTrace(identityId, ts).then((t) => setSteps(t.steps)).finally(() => setLoading(false));
    }
  }, [open, ts, identityId]);

  const statusColor: Record<string, "green" | "red" | "amber" | "gray" | "blue"> = {
    success: "green",
    error: "red",
    warn: "amber",
    info: "gray",
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title="Trace — inference pipeline">
        {loading || !steps ? (
          <div className="flex items-center gap-2 py-6 text-[var(--color-fg-subtle)]">
            <Spinner /> Loading trace…
          </div>
        ) : (
          <div className="space-y-1.5">
            {steps.map((s, i) => (
              <div key={i} className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-2.5">
                <div className="flex items-center gap-2">
                  <Badge color={statusColor[s.status] ?? "gray"}>{s.status}</Badge>
                  <span className="text-[0.82rem] font-medium">{s.stage}</span>
                  <span className="ml-auto font-mono text-[0.7rem] text-[var(--color-fg-subtle)]">{s.elapsed_ms}ms</span>
                </div>
                <div className="mt-1.5">
                  <JsonView data={s.data} maxHeight="200px" collapsed />
                </div>
              </div>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function LogsTab() {
  const identityId = useSettings((s) => s.identityId);
  const { data, isLoading } = useHistory(identityId);
  const { data: traceIdx } = useTraceIndex(identityId);
  const qc = useQueryClient();

  const [view, setView] = useState<"structured" | "raw">("structured");
  const [entries, setEntries] = useState<Message[]>([]);
  const [rawText, setRawText] = useState("");
  const [editIdx, setEditIdx] = useState<number | null>(null);
  const [editVal, setEditVal] = useState("");
  const [traceTs, setTraceTs] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const msgs = data?.messages ?? [];
    setEntries(msgs);
    setRawText(JSON.stringify(msgs.map((m) => ({ timestamp: m.timestamp, role: m.role, content: m.content })), null, 2));
  }, [data]);

  const traceSet = new Set(traceIdx?.timestamps ?? []);

  // Display newest-first — reverse index mapping so the virtualizer shows
  // the most recent message at the top without mutating the source array.
  const displayCount = entries.length;

  // Virtualize the (potentially thousands of) message rows so only what's on
  // screen is mounted. Rows vary in height, so the virtualizer measures each one
  // (ResizeObserver-backed) instead of assuming a fixed size.
  const scrollParentRef = useRef<HTMLDivElement>(null);
  const rowVirtualizer = useVirtualizer({
    count: displayCount,
    getScrollElement: () => scrollParentRef.current,
    estimateSize: () => 88,
    overscan: 8,
    paddingStart: 12,
    paddingEnd: 12,
  });

  async function persist(next: Message[]) {
    setSaving(true);
    try {
      await api.saveMessages(identityId, next);
      qc.invalidateQueries({ queryKey: ["history", identityId] });
      qc.invalidateQueries({ queryKey: ["identities"] });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function deleteMsg(idx: number) {
    if (!confirm("Delete this message?")) return;
    const next = entries.filter((_, i) => i !== idx);
    setEntries(next);
    await persist(next);
  }

  async function saveEdit(idx: number) {
    const next = entries.map((m, i) => (i === idx ? { ...m, content: editVal } : m));
    setEntries(next);
    setEditIdx(null);
    await persist(next);
  }

  async function saveRaw() {
    let parsed: Message[];
    try {
      parsed = JSON.parse(rawText);
      if (!Array.isArray(parsed)) throw new Error("Must be a JSON array");
    } catch (e) {
      toast.error("Invalid JSON: " + (e as Error).message);
      return;
    }
    await persist(parsed);
  }

  return (
    <div className="flex h-full flex-col">
      {/* Fixed header — stays put while the list below scrolls */}
      <div className="shrink-0 border-b border-[var(--color-border)] px-5 py-4">
        <div className="mx-auto flex max-w-4xl items-center justify-between gap-2">
          <div className="min-w-0">
            <h2 className="text-[1rem] font-semibold">Logs</h2>
            <p className="break-words text-[0.78rem] text-[var(--color-fg-subtle)]">
              Raw conversation history for <span className="font-mono">{identityId}</span> — edit, delete, inspect traces.
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {saving && <Spinner />}
            <div className="flex rounded-lg border border-[var(--color-border)] p-0.5">
              {(["structured", "raw"] as const).map((v) => (
                <button
                  key={v}
                  onClick={() => setView(v)}
                  className={cn(
                    "rounded-md px-2.5 py-1 text-[0.78rem] capitalize",
                    view === v ? "bg-[var(--color-surface-2)] text-[var(--color-fg)]" : "text-[var(--color-fg-subtle)]",
                  )}
                >
                  {v}
                </button>
              ))}
            </div>
            <Button size="sm" variant="secondary" onClick={() => downloadJSON(`falcon_logs_${identityId}.json`, entries)}>
              <Download className="h-3.5 w-3.5" /> Export
            </Button>
          </div>
        </div>
      </div>

      {/* Scroll container — this is the virtualizer's scroll element */}
      <div ref={scrollParentRef} className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl px-5">
          {isLoading ? (
            <div className="flex items-center gap-2 py-4 text-[var(--color-fg-subtle)]">
              <Spinner /> Loading…
            </div>
          ) : view === "raw" ? (
            <div className="space-y-2 py-4">
              <Textarea rows={20} value={rawText} onChange={(e) => setRawText(e.target.value)} className="font-mono text-[0.72rem]" />
              <Button size="sm" variant="primary" onClick={saveRaw}>
                Save raw JSON
              </Button>
            </div>
          ) : entries.length === 0 ? (
            <p className="py-4 text-[0.85rem] text-[var(--color-fg-subtle)]">No messages.</p>
          ) : (
            <div style={{ height: rowVirtualizer.getTotalSize(), position: "relative" }}>
              {rowVirtualizer.getVirtualItems().map((vi) => {
                // vi.index 0 = newest message (reversed display)
                const idx = entries.length - 1 - vi.index;
                const m = entries[idx];
                return (
                  <div
                    key={vi.key}
                    data-index={vi.index}
                    ref={rowVirtualizer.measureElement}
                    style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${vi.start}px)` }}
                  >
                    <div className="pb-1.5">
                      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-2.5">
                        <div className="mb-1 flex items-center gap-2">
                          <Badge color={m.role === "user" ? "blue" : "green"}>{m.role}</Badge>
                          <span className="font-mono text-[0.7rem] text-[var(--color-fg-subtle)]">{fmtTime(m.timestamp)}</span>
                          <span className="ml-auto flex items-center gap-1">
                            {m.role === "user" && m.timestamp && traceSet.has(m.timestamp) && (
                              <button
                                onClick={() => setTraceTs(m.timestamp)}
                                className="inline-flex items-center gap-1 rounded px-1.5 py-1 text-[0.72rem] text-[var(--color-fg-subtle)] hover:text-[var(--color-fg)]"
                              >
                                <ScrollText className="h-3.5 w-3.5" /> trace
                              </button>
                            )}
                            <button
                              onClick={() => {
                                setEditIdx(idx);
                                setEditVal(m.content);
                              }}
                              className="rounded px-1.5 py-1 text-[0.72rem] text-[var(--color-fg-subtle)] hover:text-[var(--color-fg)]"
                            >
                              edit
                            </button>
                            <button onClick={() => deleteMsg(idx)} className="rounded p-1 text-[var(--color-fg-subtle)] hover:text-[var(--color-red)]">
                              <Trash2 className="h-3.5 w-3.5" />
                            </button>
                          </span>
                        </div>
                        {editIdx === idx ? (
                          <div className="space-y-2">
                            <Textarea rows={3} value={editVal} onChange={(e) => setEditVal(e.target.value)} />
                            <div className="flex gap-2">
                              <Button size="sm" variant="primary" onClick={() => saveEdit(idx)}>
                                Save
                              </Button>
                              <Button size="sm" variant="ghost" onClick={() => setEditIdx(null)}>
                                Cancel
                              </Button>
                            </div>
                          </div>
                        ) : (
                          <div className="whitespace-pre-wrap text-[0.82rem] text-[var(--color-fg)]">{m.content}</div>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {traceTs && (
        <TraceDialog identityId={identityId} ts={traceTs} open={!!traceTs} onOpenChange={(v) => !v && setTraceTs(null)} />
      )}
    </div>
  );
}
