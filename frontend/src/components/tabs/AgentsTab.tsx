"use client";

/**
 * AgentsTab — run a watcher agent directly, without going through the chat.
 *
 * The watcher normally reaches a tool one way: the model writes an [AGENT: …]
 * block and a background thread dispatches it. That is fine when the assistant
 * is doing the work, and useless when you want to fetch one URL, look up a
 * storage id, or find out whether a tool actually works — you end up phrasing a
 * message hoping the model emits the right command.
 *
 * This tab dispatches the same tools through the same registry, with the model
 * taken out of the loop. Each agent's description, payload format and example
 * come from the map that describes it to the model, so what you read here is
 * what the model is told.
 *
 * Hidden unless the admin grants the "agents" feature; the server refuses a run
 * from an account without it, so hiding the tab is presentation, not the lock.
 */

import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ChevronRight,
  Play,
  RefreshCw,
  Search,
  Terminal,
  Wand2,
} from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useWatcherAgents, qk } from "@/lib/queries";
import { useSettings } from "@/lib/store";
import { useAuth } from "@/lib/authStore";
import type { WatcherAgent, WatcherAgentRun } from "@/lib/types";
import { Badge, Button, Input, Spinner, Textarea } from "@/components/ui/primitives";
import { Markdown } from "@/components/Markdown";
import { TweetConfirmCard, TWEET_CONFIRM_RE } from "@/components/chat/ChatMessage";
import { ResearchJobList } from "@/components/research/ResearchReports";
import { toast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";

/** True for tools documented as taking no payload — the box is pointless. */
function takesNoPayload(agent: WatcherAgent): boolean {
  return (agent.payload_hint ?? "").trim().toLowerCase().startsWith("none");
}

export function AgentsTab() {
  const { data, isLoading, isError, error, refetch, isFetching } = useWatcherAgents(true);
  const identityId = useSettings((s) => s.identityId);
  const { user } = useAuth();
  const qc = useQueryClient();

  const agents = useMemo(() => data?.agents ?? [], [data]);
  const [query, setQuery] = useState("");
  const [selectedName, setSelectedName] = useState<string | null>(null);
  // Drafts and results are kept per agent, so flicking between two tools to
  // compare their output does not throw away either one.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [runs, setRuns] = useState<Record<string, WatcherAgentRun>>({});
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState(false);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return agents;
    return agents.filter((a) =>
      `${a.name} ${a.use_when ?? ""} ${a.summary ?? ""}`.toLowerCase().includes(q),
    );
  }, [agents, query]);

  // Keep a valid selection: on first load, after a filter that excludes the
  // current pick, and after an agent is deleted elsewhere.
  useEffect(() => {
    if (filtered.length === 0) return;
    if (!selectedName || !filtered.some((a) => a.name === selectedName)) {
      setSelectedName(filtered[0].name);
    }
  }, [filtered, selectedName]);

  const selected = agents.find((a) => a.name === selectedName) ?? null;
  const draft = selected ? drafts[selected.name] ?? "" : "";
  const lastRun = selected ? runs[selected.name] : undefined;

  function setDraft(name: string, value: string) {
    setDrafts((d) => ({ ...d, [name]: value }));
    setArmed(false);  // the text changed — re-confirm before destroying anything
  }

  function select(name: string) {
    setSelectedName(name);
    setArmed(false);
  }

  async function run() {
    if (!selected || busy) return;
    // Destructive tools take two presses. There is no undo behind them, and a
    // Run button one click away from a storage id is exactly how the wrong
    // document goes.
    if (selected.destructive && !armed) {
      setArmed(true);
      return;
    }
    setArmed(false);
    setBusy(true);
    try {
      const res = await api.runWatcherAgent(selected.name, {
        payload: draft,
        identity_id: identityId,
      });
      setRuns((r) => ({ ...r, [selected.name]: res }));
      if (res.error) toast.error(`${selected.name} returned an error — see the result.`);
      // A research run has only just queued the job; the list below needs to
      // pick it up rather than wait out its polling interval.
      if (selected.name === "research") {
        qc.invalidateQueries({ queryKey: qk.researchJobs(identityId) });
      }
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* ── Header ── */}
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-[var(--color-border)] px-4 py-2.5">
        <Terminal className="h-4 w-4 shrink-0 text-[var(--color-fg-muted)]" />
        <span className="text-[0.85rem] font-semibold text-[var(--color-fg)]">Watcher Agents</span>
        <span className="hidden text-[0.72rem] text-[var(--color-fg-subtle)] sm:inline">
          Run a tool directly — no chat message, no model deciding.
        </span>
        <div className="ml-auto flex items-center gap-2">
          <span
            className="font-mono text-[0.68rem] text-[var(--color-fg-subtle)]"
            title="Runs act on this identity's documents and jobs."
          >
            {identityId}
          </span>
          <Button size="icon" variant="ghost" onClick={() => refetch()} title="Reload agents">
            <RefreshCw className={cn("h-4 w-4", isFetching && "spin")} />
          </Button>
        </div>
      </div>

      {isError ? (
        <div className="p-6 text-[0.85rem] text-[var(--color-red)]">
          {(error as Error)?.message ?? "Could not load agents."}
        </div>
      ) : isLoading ? (
        <div className="flex items-center gap-2 p-6 text-[var(--color-fg-subtle)]">
          <Spinner /> Loading agents…
        </div>
      ) : agents.length === 0 ? (
        <div className="p-6 text-[0.85rem] text-[var(--color-fg-subtle)]">
          No agents are registered.
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col sm:flex-row">
          {/* ── Agent list ── */}
          <div className="flex min-h-0 shrink-0 flex-col border-b border-[var(--color-border)] sm:w-64 sm:border-b-0 sm:border-r">
            <div className="relative shrink-0 p-2">
              <Search className="pointer-events-none absolute left-4 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--color-fg-subtle)]" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Filter agents…"
                className="h-8 pl-7 text-[0.8rem]"
              />
            </div>
            <div className="max-h-56 min-h-0 flex-1 overflow-y-auto px-2 pb-2 sm:max-h-none">
              {filtered.length === 0 ? (
                <p className="px-1 py-3 text-[0.75rem] text-[var(--color-fg-subtle)]">
                  Nothing matches “{query}”.
                </p>
              ) : (
                filtered.map((a) => (
                  <button
                    key={a.name}
                    onClick={() => select(a.name)}
                    className={cn(
                      "flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-left transition-colors",
                      a.name === selectedName
                        ? "bg-[var(--color-surface-2)] text-[var(--color-fg)]"
                        : "text-[var(--color-fg-muted)] hover:bg-[var(--color-surface)] hover:text-[var(--color-fg)]",
                    )}
                  >
                    <span className="truncate font-mono text-[0.78rem]">{a.name}</span>
                    {a.destructive && (
                      <AlertTriangle
                        className="h-3 w-3 shrink-0 text-[var(--color-red)]"
                        aria-label="Destructive"
                      />
                    )}
                    {a.kind === "generated" && (
                      <Wand2
                        className="h-3 w-3 shrink-0 text-[var(--color-fg-subtle)]"
                        aria-label="Spawned at runtime"
                      />
                    )}
                    {runs[a.name] && (
                      <span
                        className={cn(
                          "ml-auto h-1.5 w-1.5 shrink-0 rounded-full",
                          runs[a.name].error ? "bg-[var(--color-red)]" : "bg-[var(--color-green)]",
                        )}
                      />
                    )}
                  </button>
                ))
              )}
            </div>
          </div>

          {/* ── Run panel ── */}
          <div className="min-h-0 flex-1 overflow-y-auto">
            {selected && (
              <div className="space-y-4 p-4">
                {/* Identity + description */}
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="font-mono text-[0.95rem] font-semibold text-[var(--color-fg)]">
                      {selected.name}
                    </h3>
                    <Badge color={selected.kind === "generated" ? "blue" : "gray"}>
                      {selected.kind}
                    </Badge>
                    {selected.destructive && <Badge color="red">destructive</Badge>}
                  </div>
                  {(selected.use_when || selected.summary) && (
                    <p className="mt-1.5 max-w-3xl whitespace-pre-line text-[0.8rem] leading-relaxed text-[var(--color-fg-muted)]">
                      {selected.use_when || selected.summary}
                    </p>
                  )}
                </div>

                {/* Payload */}
                {takesNoPayload(selected) ? (
                  <p className="text-[0.78rem] text-[var(--color-fg-subtle)]">
                    This agent takes no input.
                  </p>
                ) : (
                  <div>
                    <div className="mb-1 flex flex-wrap items-baseline gap-2">
                      <span className="text-[0.66rem] font-semibold uppercase tracking-[0.08em] text-[var(--color-fg-muted)]">
                        Payload
                      </span>
                      {selected.payload_hint && (
                        <span className="text-[0.72rem] text-[var(--color-fg-subtle)]">
                          {selected.payload_hint}
                        </span>
                      )}
                      {selected.example && (
                        <button
                          onClick={() => setDraft(selected.name, selected.example ?? "")}
                          className="ml-auto text-[0.7rem] text-[var(--color-fg-subtle)] underline decoration-dotted hover:text-[var(--color-fg)]"
                        >
                          use example
                        </button>
                      )}
                    </div>
                    <Textarea
                      rows={selected.name === "library_store" ? 8 : 4}
                      value={draft}
                      onChange={(e) => setDraft(selected.name, e.target.value)}
                      onKeyDown={(e) => {
                        if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                          e.preventDefault();
                          run();
                        }
                      }}
                      placeholder={selected.example || "Payload passed to the tool…"}
                      disabled={busy}
                      className="font-mono text-[0.8rem]"
                    />
                  </div>
                )}

                {/* Run */}
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    onClick={run}
                    loading={busy}
                    variant={armed ? "secondary" : "primary"}
                    className={cn(
                      armed &&
                        "border-[var(--color-red)] text-[var(--color-red)] hover:border-[var(--color-red)] hover:bg-[var(--color-red)]/10 hover:text-[var(--color-red)]",
                    )}
                    disabled={busy}
                  >
                    {!busy && <Play className="h-3.5 w-3.5" />}
                    {busy
                      ? "Running…"
                      : armed
                        ? `Yes — run ${selected.name}`
                        : `Run ${selected.name}`}
                  </Button>
                  {armed && (
                    <Button variant="ghost" size="sm" onClick={() => setArmed(false)}>
                      Cancel
                    </Button>
                  )}
                  <span className="text-[0.7rem] text-[var(--color-fg-subtle)]">
                    {armed
                      ? "This permanently deletes what the payload names. There is no undo."
                      : busy
                        ? "The tool is running on the server — leaving this tab will not stop it."
                        : "Ctrl+Enter also runs. Every run is recorded in the watcher log."}
                  </span>
                </div>

                {/* Result */}
                {lastRun && <RunResult run={lastRun} identityId={identityId} />}

                {/* research answers immediately with a job id and produces its
                    report minutes later, so the run result alone is never the
                    output. Every job for this identity is listed here. */}
                {selected.name === "research" && (
                  <div className="border-t border-[var(--color-border)] pt-4">
                    <div className="mb-2 flex items-baseline gap-2">
                      <span className="text-[0.66rem] font-semibold uppercase tracking-[0.08em] text-[var(--color-fg-muted)]">
                        Reports
                      </span>
                      <span className="text-[0.7rem] text-[var(--color-fg-subtle)]">
                        every research job for {identityId}
                      </span>
                    </div>
                    <ResearchJobList
                      identityId={identityId}
                      emptyHint="No research jobs yet. Ask a question above — it runs in the background for a few minutes, and the report appears here when it is done."
                    />
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Admins run as whichever identity is selected in the sidebar; portal
          users are pinned server-side to their own regardless of this. */}
      {user?.role === "admin" && (
        <div className="shrink-0 border-t border-[var(--color-border)] px-4 py-1.5 text-[0.68rem] text-[var(--color-fg-subtle)]">
          Running as <span className="font-mono">{identityId}</span> — change the identity in the
          sidebar to act on another user&rsquo;s documents.
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Result
// ---------------------------------------------------------------------------

function RunResult({ run, identityId }: { run: WatcherAgentRun; identityId: string }) {
  const [raw, setRaw] = useState(false);

  // post_tweet stages rather than posts, and marks the result with a code. The
  // same approval card the chat shows is rendered here, so a tweet staged from
  // this tab can be read, edited and posted without going to find the chat.
  const staged = run.result.match(TWEET_CONFIRM_RE);
  const body = staged ? run.result.replace(TWEET_CONFIRM_RE, "").trim() : run.result;

  return (
    <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]">
      <div className="flex flex-wrap items-center gap-2 border-b border-[var(--color-border)] px-3 py-2">
        <Badge color={run.error ? "red" : "green"}>{run.error ? "error" : "ok"}</Badge>
        <span className="font-mono text-[0.72rem] text-[var(--color-fg-subtle)]">
          {run.latency_ms} ms
        </span>
        <button
          onClick={() => setRaw((v) => !v)}
          className="ml-auto text-[0.7rem] text-[var(--color-fg-subtle)] underline decoration-dotted hover:text-[var(--color-fg)]"
        >
          {raw ? "rendered" : "raw"}
        </button>
      </div>

      <div className="p-3">
        {raw ? (
          <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap break-words font-mono text-[0.75rem] text-[var(--color-fg)]">
            {run.result}
          </pre>
        ) : (
          <div className="max-h-[28rem] overflow-auto">
            <Markdown className="text-[0.85rem]">{body}</Markdown>
          </div>
        )}

        {staged && (
          <div className="mt-3 border-t border-[var(--color-border)] pt-3">
            <p className="mb-2 flex items-center gap-1.5 text-[0.72rem] text-[var(--color-fg-muted)]">
              <ChevronRight className="h-3 w-3" />
              Nothing has been posted. This tweet is staged until you approve it.
            </p>
            <TweetConfirmCard identityId={identityId} code={staged[1]} />
          </div>
        )}
      </div>
    </div>
  );
}
