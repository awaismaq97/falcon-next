"use client";

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Download, Trash2, ScrollText, Bot, ChevronDown, ChevronRight, Lock } from "lucide-react";
import { api } from "@/lib/api";
import {
  useHistory,
  useTraceIndex,
  useWatcherStatus,
  useWatcherLog,
  useWatcherAgents,
  qk,
} from "@/lib/queries";
import { useSettings } from "@/lib/store";
import type { Message, TraceStep, WatcherLogEntry, WatcherAgent } from "@/lib/types";
import { Button, Textarea, Badge, Spinner } from "@/components/ui/primitives";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { JsonView } from "@/components/JsonView";
import { cn, downloadJSON, fmtTime } from "@/lib/utils";
import { toast } from "@/components/ui/toast";

// ---------------------------------------------------------------------------
// Watcher log panel
// ---------------------------------------------------------------------------

function WatcherLogEntry({ entry }: { entry: WatcherLogEntry }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-2.5">
      <div className="flex items-center gap-2">
        <Badge color={entry.error ? "red" : "green"}>
          {entry.error ? "error" : "ok"}
        </Badge>
        <span className="font-mono text-[0.8rem] font-medium text-[var(--color-fg)]">
          {entry.command}
        </span>
        <span className="ml-auto flex items-center gap-2">
          <span className="font-mono text-[0.68rem] text-[var(--color-fg-subtle)]">
            {entry.latency_ms}ms
          </span>
          <span className="font-mono text-[0.68rem] text-[var(--color-fg-subtle)]">
            {fmtTime(entry.recorded_at)}
          </span>
          <button
            onClick={() => setOpen((o) => !o)}
            className="rounded p-0.5 text-[var(--color-fg-subtle)] hover:text-[var(--color-fg)]"
          >
            {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
          </button>
        </span>
      </div>
      {open && (
        <div className="mt-2 space-y-1.5 border-t border-[var(--color-border)] pt-2">
          {entry.payload && (
            <div>
              <div className="mb-0.5 text-[0.68rem] font-semibold uppercase tracking-wide text-[var(--color-fg-subtle)]">
                Payload
              </div>
              <pre className="whitespace-pre-wrap break-words rounded bg-[var(--color-surface-2)] px-2 py-1.5 font-mono text-[0.72rem] text-[var(--color-fg)]">
                {entry.payload}
              </pre>
            </div>
          )}
          <div>
            <div className="mb-0.5 text-[0.68rem] font-semibold uppercase tracking-wide text-[var(--color-fg-subtle)]">
              Result
            </div>
            <pre className="whitespace-pre-wrap break-words rounded bg-[var(--color-surface-2)] px-2 py-1.5 font-mono text-[0.72rem] text-[var(--color-fg)]">
              {entry.result}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Agent manager — view and delete the tools the watcher can dispatch
// ---------------------------------------------------------------------------

function AgentRow({ agent, onDeleted }: { agent: WatcherAgent; onDeleted: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  async function remove() {
    if (
      !confirm(
        `Delete agent "${agent.name}"?\n\nThis removes its stored code, unregisters it from the watcher, ` +
          `and drops it from the persona so the model stops being told it exists. This cannot be undone.`,
      )
    )
      return;
    setBusy(true);
    try {
      await api.deleteWatcherAgent(agent.name);
      toast.success(`Agent "${agent.name}" deleted.`);
      onDeleted();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-2.5">
      <div className="flex items-center gap-2">
        <Badge color={agent.kind === "generated" ? "blue" : "gray"}>{agent.kind}</Badge>
        <span className="font-mono text-[0.8rem] font-medium text-[var(--color-fg)]">
          {agent.name}
        </span>
        {agent.revision != null && agent.revision > 1 && (
          <span className="font-mono text-[0.68rem] text-[var(--color-fg-subtle)]">
            rev {agent.revision}
          </span>
        )}
        <span className="ml-auto flex items-center gap-2">
          {agent.created_at && (
            <span className="font-mono text-[0.68rem] text-[var(--color-fg-subtle)]">
              {fmtTime(agent.created_at)}
            </span>
          )}
          {agent.deletable ? (
            <Button size="sm" variant="ghost" onClick={remove} disabled={busy}>
              {busy ? <Spinner /> : <Trash2 className="h-3.5 w-3.5" />}
            </Button>
          ) : (
            // Built-ins live in source and spawn_agent is the only way to make
            // new tools — neither can be removed from here.
            <span
              title={
                agent.name === "spawn_agent"
                  ? "Protected — spawn_agent is the only way to create new agents."
                  : "Built-in tool — defined in code, not deletable."
              }
              className="text-[var(--color-fg-subtle)]"
            >
              <Lock className="h-3.5 w-3.5" />
            </span>
          )}
          <button
            onClick={() => setOpen((o) => !o)}
            className="rounded p-0.5 text-[var(--color-fg-subtle)] hover:text-[var(--color-fg)]"
          >
            {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
          </button>
        </span>
      </div>

      {open && (
        <div className="mt-2 space-y-1.5 border-t border-[var(--color-border)] pt-2">
          {agent.summary && (
            <div>
              <div className="mb-0.5 text-[0.68rem] font-semibold uppercase tracking-wide text-[var(--color-fg-subtle)]">
                {agent.kind === "generated" ? "Spawn prompt" : "Description"}
              </div>
              <p className="text-[0.75rem] text-[var(--color-fg)]">{agent.summary}</p>
            </div>
          )}
          {agent.code && (
            <div>
              <div className="mb-0.5 text-[0.68rem] font-semibold uppercase tracking-wide text-[var(--color-fg-subtle)]">
                Source
              </div>
              <pre className="max-h-72 overflow-auto whitespace-pre rounded bg-[var(--color-surface-2)] px-2 py-1.5 font-mono text-[0.72rem] text-[var(--color-fg)]">
                {agent.code}
              </pre>
            </div>
          )}
          {!agent.code && agent.kind === "builtin" && (
            <p className="text-[0.72rem] text-[var(--color-fg-subtle)]">
              Defined in <span className="font-mono">watcher_tools.py</span>.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function AgentManagerDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const qc = useQueryClient();
  // Only fetch while the dialog is actually open.
  const { data, isLoading } = useWatcherAgents(open);
  const agents = data?.agents ?? [];
  const generated = agents.filter((a) => a.kind === "generated").length;

  function refresh() {
    qc.invalidateQueries({ queryKey: qk.watcherAgents() });
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title="Watcher agents">
        {isLoading ? (
          <div className="flex items-center gap-2 py-6 text-[var(--color-fg-subtle)]">
            <Spinner /> Loading agents…
          </div>
        ) : agents.length === 0 ? (
          <p className="py-4 text-[0.82rem] text-[var(--color-fg-subtle)]">
            No agents registered.
          </p>
        ) : (
          <>
            <p className="mb-3 text-[0.75rem] text-[var(--color-fg-subtle)]">
              {agents.length} registered · {generated} spawned at runtime. Deleting a spawned agent
              removes its code and its persona entry, so the model stops being told it exists.
            </p>
            <div className="space-y-1.5">
              {agents.map((a) => (
                <AgentRow key={a.name} agent={a} onDeleted={refresh} />
              ))}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function WatcherLogPanel({ identityId, hideHeader }: { identityId: string; hideHeader?: boolean }) {
  const qc = useQueryClient();
  const { data: statusData } = useWatcherStatus(identityId);
  const { data: logData, isLoading } = useWatcherLog(identityId, 50);
  const [agentsOpen, setAgentsOpen] = useState(false);

  async function clearLog() {
    if (!confirm("Clear watcher log for this identity?")) return;
    try {
      await api.clearWatcherLog(identityId);
      qc.invalidateQueries({ queryKey: qk.watcherLog(identityId) });
      toast.success("Watcher log cleared.");
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  const records = logData?.records ?? [];

  return (
    <div className={hideHeader ? "" : "mt-6"}>
      {!hideHeader && (
        /* Section header — only shown when NOT inside the drawer (drawer has its own header) */
        <div className="mb-3 flex items-center gap-2">
          <Bot className="h-4 w-4 text-[var(--color-fg-subtle)]" />
          <h3 className="text-[0.9rem] font-semibold">Watcher Agent Log</h3>
          <WatcherStatusDot identityId={identityId} />
          <div className="ml-auto flex items-center gap-2">
            <Button size="sm" variant="ghost" onClick={() => setAgentsOpen(true)}>
              <Bot className="h-3.5 w-3.5" /> Agents
            </Button>
            {records.length > 0 && (
              <Button size="sm" variant="ghost" onClick={clearLog}>
                <Trash2 className="h-3.5 w-3.5" /> Clear
              </Button>
            )}
            <Button
              size="sm"
              variant="secondary"
              onClick={() => downloadJSON(`falcon_watcher_log_${identityId}.json`, records)}
              disabled={records.length === 0}
            >
              <Download className="h-3.5 w-3.5" /> Export
            </Button>
          </div>
        </div>
      )}

      {/* Actions row when inside drawer (hideHeader=true). Always rendered so
          Agents stays reachable even before the first invocation is logged. */}
      {hideHeader && (
        <div className="flex items-center justify-end gap-2 border-b border-[var(--color-border)] px-4 py-1">
          <Button size="sm" variant="ghost" onClick={() => setAgentsOpen(true)}>
            <Bot className="h-3.5 w-3.5" /> Agents
          </Button>
          {records.length > 0 && (
            <>
              <Button size="sm" variant="ghost" onClick={clearLog}>
                <Trash2 className="h-3.5 w-3.5" /> Clear
              </Button>
              <Button
                size="sm"
                variant="secondary"
                onClick={() => downloadJSON(`falcon_watcher_log_${identityId}.json`, records)}
              >
                <Download className="h-3.5 w-3.5" /> Export
              </Button>
            </>
          )}
        </div>
      )}

      {isLoading ? (
        <div className="flex items-center gap-2 px-4 py-3 text-[var(--color-fg-subtle)]">
          <Spinner /> Loading…
        </div>
      ) : records.length === 0 ? (
        <p className="px-4 py-3 text-[0.82rem] text-[var(--color-fg-subtle)]">
          No watcher invocations yet.{" "}
          {!statusData?.enabled && (
            <span>Enable the watcher in Admin Panel → Users tab.</span>
          )}
        </p>
      ) : (
        <div className="space-y-1.5 px-4 py-2">
          {records.map((r, i) => (
            <WatcherLogEntry key={i} entry={r} />
          ))}
        </div>
      )}

      <AgentManagerDialog open={agentsOpen} onOpenChange={setAgentsOpen} />
    </div>
  );
}

function WatcherStatusDot({ identityId }: { identityId: string }) {
  const { data: statusData } = useWatcherStatus(identityId);
  return (
    <span className="flex items-center gap-1.5 rounded-full border border-[var(--color-border)] px-2 py-0.5 text-[0.7rem]">
      <span
        className={cn(
          "inline-block h-1.5 w-1.5 rounded-full",
          statusData?.running
            ? "bg-green-500"
            : statusData?.enabled
            ? "bg-amber-400"
            : "bg-[var(--color-fg-subtle)]",
        )}
      />
      {statusData?.running ? "running" : statusData?.enabled ? "starting" : "disabled"}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Trace dialog
// ---------------------------------------------------------------------------

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

  // Watcher drawer state
  const DRAWER_MIN = 44;   // collapsed — just the header bar visible
  const DRAWER_MAX = 600;  // max draggable height
  const DRAWER_DEFAULT = 220;
  const [drawerHeight, setDrawerHeight] = useState(DRAWER_DEFAULT);
  const [drawerOpen, setDrawerOpen] = useState(true);
  const dragRef = useRef<{ startY: number; startH: number } | null>(null);

  function onDragStart(e: React.MouseEvent) {
    e.preventDefault();
    dragRef.current = { startY: e.clientY, startH: drawerHeight };
    const onMove = (ev: MouseEvent) => {
      if (!dragRef.current) return;
      const delta = dragRef.current.startY - ev.clientY; // drag up = larger
      const next = Math.min(DRAWER_MAX, Math.max(DRAWER_MIN, dragRef.current.startH + delta));
      setDrawerHeight(next);
      setDrawerOpen(next > DRAWER_MIN + 10);
    };
    const onUp = () => {
      dragRef.current = null;
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  function toggleDrawer() {
    if (drawerOpen) {
      setDrawerHeight(DRAWER_MIN);
      setDrawerOpen(false);
    } else {
      setDrawerHeight(DRAWER_DEFAULT);
      setDrawerOpen(true);
    }
  }

  useEffect(() => {
    const msgs = data?.messages ?? [];
    setEntries(msgs);
    setRawText(JSON.stringify(msgs.map((m) => ({ timestamp: m.timestamp, role: m.role, content: m.content })), null, 2));
  }, [data]);

  const traceSet = new Set(traceIdx?.timestamps ?? []);
  const displayCount = entries.length;

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
    <div className="flex h-full flex-col overflow-hidden">
      {/* ── Fixed header ── */}
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

      {/* ── Message list — fills remaining space above the drawer ── */}
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
                              onClick={() => { setEditIdx(idx); setEditVal(m.content); }}
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
                              <Button size="sm" variant="primary" onClick={() => saveEdit(idx)}>Save</Button>
                              <Button size="sm" variant="ghost" onClick={() => setEditIdx(null)}>Cancel</Button>
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

      {/* ── Watcher drawer — resizable bottom panel ── */}
      <div
        className="shrink-0 border-t border-[var(--color-border)] bg-[var(--color-bg)] flex flex-col"
        style={{ height: drawerHeight, transition: dragRef.current ? "none" : "height 0.15s ease" }}
      >
        {/* Drag handle */}
        <div
          onMouseDown={onDragStart}
          className="flex h-1.5 w-full cursor-row-resize items-center justify-center group"
          title="Drag to resize"
        >
          <div className="h-1 w-10 rounded-full bg-[var(--color-border)] group-hover:bg-[var(--color-fg-subtle)] transition-colors" />
        </div>

        {/* Drawer header */}
        <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] px-4 py-1.5">
          <Bot className="h-3.5 w-3.5 text-[var(--color-fg-subtle)]" />
          <span className="text-[0.8rem] font-semibold">Watcher Agent Log</span>
          {/* Running indicator */}
          <WatcherStatusDot identityId={identityId} />
          {/* Toggle collapse / expand */}
          <button
            onClick={toggleDrawer}
            className="ml-auto rounded p-1 text-[var(--color-fg-subtle)] hover:text-[var(--color-fg)] transition-colors"
            title={drawerOpen ? "Collapse" : "Expand"}
          >
            <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", !drawerOpen && "rotate-180")} />
          </button>
        </div>

        {/* Drawer body — scrollable log list */}
        {drawerOpen && (
          <div className="min-h-0 flex-1 overflow-y-auto">
            <WatcherLogPanel identityId={identityId} hideHeader />
          </div>
        )}
      </div>

      {traceTs && (
        <TraceDialog identityId={identityId} ts={traceTs} open={!!traceTs} onOpenChange={(v) => !v && setTraceTs(null)} />
      )}
    </div>
  );
}
