"use client";

/**
 * ResearchReports — the output of the `research` agent, wherever it is shown.
 *
 * A research job outlives the message that started it: it runs for minutes,
 * survives restarts, and is kept until someone deletes it. So its report needs
 * somewhere to live that is not a chat message — the Logs tab has shown it in a
 * dialog, and the Watcher Agents tab shows it under the agent that produces it.
 * Both render this, so a report reads the same and a job deleted in one place is
 * gone in the other.
 */

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Ban, ChevronDown, ChevronRight, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { useResearchJob, useResearchJobs, qk } from "@/lib/queries";
import type { ResearchJobSummary } from "@/lib/types";
import { Badge, Button, Spinner } from "@/components/ui/primitives";
import { Markdown } from "@/components/Markdown";
import { toast } from "@/components/ui/toast";
import { fmtTime } from "@/lib/utils";

const RESEARCH_STATUS_COLOR: Record<string, "green" | "blue" | "red" | "gray"> = {
  done: "green",
  running: "blue",
  queued: "gray",
  failed: "red",
  cancelled: "gray",
};

export function ResearchJobRow({
  identityId,
  job,
  onChanged,
}: {
  identityId: string;
  job: ResearchJobSummary;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  // The report body is only worth fetching once someone opens the row.
  const { data: full, isLoading } = useResearchJob(identityId, open ? job.job_id : null);
  const live = job.status === "queued" || job.status === "running";

  async function cancel() {
    if (!confirm(`Stop research job "${job.job_id}"?\n\nFindings gathered so far are kept.`)) return;
    setBusy(true);
    try {
      await api.cancelResearchJob(identityId, job.job_id);
      toast.success(`Job ${job.job_id} cancelled.`);
      onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (
      !confirm(
        `Permanently delete research job "${job.job_id}"?\n\n` +
          `"${job.question}"\n\n` +
          `This erases its report, findings and sources from the database. ` +
          `Nothing else deletes research data, so this cannot be undone.`,
      )
    )
      return;
    setBusy(true);
    try {
      await api.deleteResearchJob(identityId, job.job_id);
      toast.success(`Job ${job.job_id} deleted.`);
      onChanged();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-2.5">
      <div className="flex items-center gap-2">
        <Badge color={RESEARCH_STATUS_COLOR[job.status] ?? "gray"}>{job.status}</Badge>
        <span className="font-mono text-[0.72rem] text-[var(--color-fg-subtle)]">{job.job_id}</span>
        <span className="min-w-0 flex-1 truncate text-[0.8rem] text-[var(--color-fg)]" title={job.question}>
          {job.question}
        </span>
        <span className="flex shrink-0 items-center gap-2">
          <span className="font-mono text-[0.68rem] text-[var(--color-fg-subtle)]">
            {job.rounds_done}/{job.max_rounds} · {job.sources.length} src
          </span>
          {/* Stop is for work in progress; delete is for the record afterwards.
              Keeping them distinct means a click meant to stop a job can never
              destroy the findings it already gathered. */}
          {live ? (
            <Button size="sm" variant="ghost" onClick={cancel} disabled={busy} title="Stop this job">
              {busy ? <Spinner /> : <Ban className="h-3.5 w-3.5" />}
            </Button>
          ) : (
            <Button
              size="sm"
              variant="ghost"
              onClick={remove}
              disabled={busy}
              title="Delete this job and its report permanently"
            >
              {busy ? <Spinner /> : <Trash2 className="h-3.5 w-3.5" />}
            </Button>
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
        <div className="mt-2 border-t border-[var(--color-border)] pt-2">
          {isLoading ? (
            <div className="flex items-center gap-2 py-3 text-[0.78rem] text-[var(--color-fg-subtle)]">
              <Spinner /> Loading report…
            </div>
          ) : !full ? (
            <p className="py-2 text-[0.75rem] text-[var(--color-fg-subtle)]">Could not load this job.</p>
          ) : (
            <div className="space-y-2.5">
              {full.report ? (
                <Markdown>{full.report}</Markdown>
              ) : (
                <p className="text-[0.75rem] text-[var(--color-fg-subtle)]">
                  {live
                    ? "Still working — the report is written after the final round."
                    : "No report was produced."}
                </p>
              )}

              {full.error && (
                <p className="text-[0.75rem] text-[var(--color-red)]">Error: {full.error}</p>
              )}

              {full.findings.length > 0 && (
                <details>
                  <summary className="cursor-pointer text-[0.68rem] font-semibold uppercase tracking-wide text-[var(--color-fg-subtle)]">
                    {full.findings.length} findings
                  </summary>
                  <ul className="mt-1.5 space-y-1">
                    {full.findings.map((f, i) => (
                      <li key={i} className="text-[0.75rem] text-[var(--color-fg)]">
                        <span className="text-[var(--color-fg-subtle)]">r{f.round}</span> {f.note}{" "}
                        <a
                          href={f.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="break-all text-[0.7rem] text-[var(--color-blue)] underline"
                        >
                          {f.url}
                        </a>
                      </li>
                    ))}
                  </ul>
                </details>
              )}

              <p className="text-[0.68rem] text-[var(--color-fg-subtle)]">
                {full.provider} · started {fmtTime(full.created_at)}
                {full.finished_at ? ` · finished ${fmtTime(full.finished_at)}` : ""}
                {full.queries_run.length > 0 && ` · searched: ${full.queries_run.join(" | ")}`}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Every research job for one identity, newest first.
 *
 * `enabled` gates the fetch: the list polls while it is on screen (a running
 * job's round count changes underneath it), so a panel that is closed or on an
 * inactive tab should not be asking.
 */
export function ResearchJobList({
  identityId,
  enabled = true,
  emptyHint,
}: {
  identityId: string;
  enabled?: boolean;
  emptyHint?: string;
}) {
  const qc = useQueryClient();
  const { data, isLoading } = useResearchJobs(identityId, enabled);
  const jobs = data?.jobs ?? [];
  const running = jobs.filter((j) => j.status === "running" || j.status === "queued").length;

  function refresh() {
    qc.invalidateQueries({ queryKey: qk.researchJobs(identityId) });
  }

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 py-6 text-[var(--color-fg-subtle)]">
        <Spinner /> Loading reports…
      </div>
    );
  }

  if (jobs.length === 0) {
    return (
      <p className="py-4 text-[0.82rem] text-[var(--color-fg-subtle)]">
        {emptyHint ??
          "No research jobs yet. Ask the assistant to research something — it runs in the background and the report appears here when it is done."}
      </p>
    );
  }

  return (
    <>
      <p className="mb-3 text-[0.75rem] text-[var(--color-fg-subtle)]">
        {jobs.length} job{jobs.length === 1 ? "" : "s"}
        {running > 0 && ` · ${running} still running`}. Jobs survive restarts and are kept
        indefinitely — nothing expires them, so a report from months ago is still here until you
        delete it.
      </p>
      <div className="space-y-1.5">
        {jobs.map((j) => (
          <ResearchJobRow key={j.job_id} identityId={identityId} job={j} onChanged={refresh} />
        ))}
      </div>
    </>
  );
}
