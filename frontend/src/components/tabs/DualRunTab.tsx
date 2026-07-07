"use client";

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Download, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { useDualRuns } from "@/lib/queries";
import { useSettings } from "@/lib/store";
import type { DualRunRecord, DualRunSide } from "@/lib/types";
import { Button, Badge, Spinner, Card } from "@/components/ui/primitives";
import { cn, downloadJSON, fmtTime } from "@/lib/utils";

const STATE_COLORS: Record<string, "green" | "red" | "amber" | "blue" | "gray"> = {
  Neutral: "gray",
  Focused: "blue",
  Coherence: "green",
  "Grief process": "amber",
};

function RunSide({ label, side, sunActive }: { label: string; side: DualRunSide; sunActive: boolean }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] p-2.5">
      <div className="mb-1.5 flex items-center gap-2">
        <span className="text-[0.75rem] font-semibold text-[var(--color-fg-muted)]">{label}</span>
        {sunActive &&
          (side.broke_through ? <Badge color="red">🔴 BREAKTHROUGH</Badge> : <Badge color="green">🟢 HELD</Badge>)}
        <span className="ml-auto font-mono text-[0.68rem] text-[var(--color-fg-subtle)]">
          {side.tokens?.total_tokens ?? "?"} tok · {side.latency_ms}ms
        </span>
      </div>
      {side.broke_through && side.first_break && (
        <div className="mb-1.5 rounded-md border border-red-200 bg-red-50 px-2 py-1 text-[0.75rem] text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-400">
          First break: <span className="font-mono">{side.first_break}</span>
        </div>
      )}
      <div className="max-h-[160px] overflow-y-auto whitespace-pre-wrap text-[0.8rem] text-[var(--color-fg)]">
        {side.text}
      </div>
    </div>
  );
}

function RecordCard({ rec }: { rec: DualRunRecord }) {
  return (
    <Card>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Badge color={STATE_COLORS[rec.state_tag] ?? "gray"}>{rec.state_tag}</Badge>
        {rec.any_breakthrough ? <Badge color="red">breakthrough</Badge> : <Badge color="green">held</Badge>}
        {rec.sun_instruction_active && <Badge color="amber">☀️ active</Badge>}
        <span className="font-mono text-[0.7rem] text-[var(--color-fg-subtle)]">{fmtTime(rec.recorded_at)}</span>
        <span className="ml-auto font-mono text-[0.7rem] text-[var(--color-fg-subtle)]">
          {rec.model.split("/").pop()}
        </span>
      </div>
      <div className="mb-2 text-[0.8rem] text-[var(--color-fg-muted)]">
        <span className="text-[var(--color-fg-subtle)]">input:</span> {rec.user_input.slice(0, 160)}
      </div>
      <div className="grid gap-2 md:grid-cols-2">
        <RunSide label="Run 1" side={rec.run1} sunActive={rec.sun_instruction_active} />
        <RunSide label="Run 2" side={rec.run2} sunActive={rec.sun_instruction_active} />
      </div>
    </Card>
  );
}

export function DualRunTab() {
  const identityId = useSettings((s) => s.identityId);
  const { data, isLoading } = useDualRuns(identityId);
  const qc = useQueryClient();
  const [filter, setFilter] = useState<"all" | "breakthrough" | "held">("all");

  const records = data?.records ?? [];
  const stats = data?.stats;
  const filtered = records.filter((r) =>
    filter === "all" ? true : filter === "breakthrough" ? r.any_breakthrough : !r.any_breakthrough,
  );

  async function clearAll() {
    if (!confirm("Delete all dual-run records for this identity?")) return;
    await api.deleteDualRuns(identityId);
    qc.invalidateQueries({ queryKey: ["dual-run", identityId] });
  }

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-5">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <h2 className="text-[1rem] font-semibold">Dual-run log</h2>
          <p className="break-words text-[0.78rem] text-[var(--color-fg-subtle)]">
            Each message runs twice with an identical payload; both outputs are logged side-by-side.
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button
            size="sm"
            variant="secondary"
            onClick={async () => downloadJSON(`falcon_dualrun_${identityId}.json`, await api.exportDualRuns(identityId))}
          >
            <Download className="h-3.5 w-3.5" /> Export
          </Button>
          {records.length > 0 && (
            <Button size="sm" variant="danger" onClick={clearAll}>
              <Trash2 className="h-3.5 w-3.5" /> Clear
            </Button>
          )}
        </div>
      </div>

      {stats && stats.total_runs > 0 && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <StatBox label="Total runs" value={String(stats.total_runs)} />
          <StatBox label="Breakthroughs" value={String(stats.breakthrough_count)} />
          <StatBox label="Breakthrough rate" value={`${(stats.breakthrough_rate * 100).toFixed(0)}%`} />
          <StatBox label="States" value={String(Object.keys(stats.per_state).length)} />
        </div>
      )}

      <div className="flex gap-1">
        {(["all", "breakthrough", "held"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={cn(
              "rounded-lg border px-3 py-1 text-[0.78rem] capitalize",
              filter === f
                ? "border-[var(--color-fg)] bg-[var(--color-surface-2)] text-[var(--color-fg)]"
                : "border-[var(--color-border)] text-[var(--color-fg-subtle)]",
            )}
          >
            {f}
          </button>
        ))}
      </div>

      {isLoading ? (
        <div className="flex items-center gap-2 text-[var(--color-fg-subtle)]">
          <Spinner /> Loading dual-run records…
        </div>
      ) : filtered.length === 0 ? (
        <p className="text-[0.85rem] text-[var(--color-fg-subtle)]">
          No dual-run records{filter !== "all" ? ` (${filter})` : ""}. Enable Dual Run in the sidebar and send a message.
        </p>
      ) : (
        <div className="space-y-2">
          {filtered.map((r, i) => (
            <RecordCard key={i} rec={r} />
          ))}
        </div>
      )}
    </div>
  );
}

function StatBox({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-2.5">
      <div className="text-[0.7rem] text-[var(--color-fg-subtle)]">{label}</div>
      <div className="text-[1.2rem] font-semibold text-[var(--color-fg)]">{value}</div>
    </div>
  );
}
