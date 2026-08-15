"use client";

import { useState, useEffect, useCallback } from "react";
import { RefreshCw } from "lucide-react";
import { adminApi, type AdminAuditRecord } from "@/lib/adminApi";
import { cn } from "@/lib/utils";

const ACTION_COLORS: Record<string, string> = {
  login_success:      "bg-green-100 text-green-700 dark:bg-green-950/40 dark:text-green-300",
  login_failed:       "bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300",
  create_user:        "bg-blue-100 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300",
  update_user:        "bg-amber-100 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300",
  delete_user:        "bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300",
  update_permissions: "bg-purple-100 text-purple-700 dark:bg-purple-950/40 dark:text-purple-300",
};

export function AuditLogTab() {
  const [records, setRecords] = useState<AdminAuditRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setRecords(await adminApi.getAuditLog(200));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="p-4 sm:p-6">
      {/* Toolbar */}
      <div className="mb-4 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-[var(--color-fg)]">
          Admin Activity Log
          <span className="ml-2 text-xs font-normal text-[var(--color-fg-subtle)]">
            ({records.length})
          </span>
        </h3>
        <button
          onClick={load}
          className="rounded-lg p-2 text-[var(--color-fg-muted)] hover:bg-[var(--color-surface)] hover:text-[var(--color-fg)] transition-colors"
          title="Refresh"
        >
          <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
        </button>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </div>
      )}

      {loading && !records.length ? (
        <div className="py-12 text-center text-sm text-[var(--color-fg-subtle)]">Loading audit log…</div>
      ) : records.length === 0 ? (
        <div className="rounded-xl border border-dashed border-[var(--color-border)] py-12 text-center text-sm text-[var(--color-fg-subtle)]">
          No audit records yet.
        </div>
      ) : (
        <>
          {/* ── Mobile: card list ── */}
          <div className="flex flex-col gap-3 sm:hidden">
            {records.map((rec, i) => (
              <AuditCard key={i} rec={rec} />
            ))}
          </div>

          {/* ── Desktop: table ── */}
          <div className="hidden sm:block overflow-x-auto rounded-xl border border-[var(--color-border)]">
            <table className="w-full text-sm">
              <thead className="bg-[var(--color-surface)]">
                <tr>
                  {["Time", "Actor", "Action", "Target", "Details"].map((h) => (
                    <th key={h} className="px-4 py-3 text-left text-xs font-medium text-[var(--color-fg-muted)]">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--color-border)]">
                {records.map((rec, i) => (
                  <tr key={i} className="transition-colors hover:bg-[var(--color-surface)]/50">
                    <td className="px-4 py-3 font-mono text-[0.72rem] text-[var(--color-fg-subtle)] whitespace-nowrap">
                      {formatTime(rec.timestamp)}
                    </td>
                    <td className="px-4 py-3 font-mono text-[0.82rem] text-[var(--color-fg)]">{rec.actor}</td>
                    <td className="px-4 py-3">
                      <ActionBadge action={rec.action} />
                    </td>
                    <td className="px-4 py-3 font-mono text-[0.82rem] text-[var(--color-fg-muted)]">
                      {rec.target ?? <span className="italic text-[var(--color-fg-subtle)]">—</span>}
                    </td>
                    <td className="px-4 py-3 font-mono text-[0.72rem] text-[var(--color-fg-subtle)]">
                      {Object.keys(rec.details ?? {}).length > 0
                        ? <DetailsCell details={rec.details} />
                        : <span className="italic">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

// ── Mobile audit card ──────────────────────────────────────────────────────
function AuditCard({ rec }: { rec: AdminAuditRecord }) {
  const [detailOpen, setDetailOpen] = useState(false);
  const hasDetails = Object.keys(rec.details ?? {}).length > 0;

  return (
    <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      {/* Time + action badge */}
      <div className="mb-2 flex items-center justify-between gap-2 flex-wrap">
        <span className="font-mono text-[0.7rem] text-[var(--color-fg-subtle)]">{formatTime(rec.timestamp)}</span>
        <ActionBadge action={rec.action} />
      </div>

      {/* Actor + target */}
      <div className="mb-1 flex items-center gap-2 flex-wrap">
        <span className="font-mono text-xs font-semibold text-[var(--color-fg)]">{rec.actor}</span>
        {rec.target && (
          <>
            <span className="text-xs text-[var(--color-fg-subtle)]">→</span>
            <span className="font-mono text-xs text-[var(--color-fg-muted)]">{rec.target}</span>
          </>
        )}
      </div>

      {/* Details (expandable) */}
      {hasDetails && (
        <button
          onClick={() => setDetailOpen(v => !v)}
          className="mt-1 text-[0.7rem] text-[var(--color-fg-subtle)] underline decoration-dotted hover:text-[var(--color-fg-muted)]"
        >
          {detailOpen ? "Hide details" : "Show details"}
        </button>
      )}
      {detailOpen && hasDetails && (
        <pre className="mt-2 overflow-x-auto rounded-lg bg-[var(--color-bg)] p-2 text-[0.68rem] text-[var(--color-fg-subtle)] whitespace-pre-wrap">
          {JSON.stringify(rec.details, null, 2)}
        </pre>
      )}
    </div>
  );
}

// ── Shared ────────────────────────────────────────────────────────────────
function ActionBadge({ action }: { action: string }) {
  return (
    <span className={cn(
      "inline-block rounded-full px-2.5 py-0.5 text-[0.72rem] font-medium whitespace-nowrap",
      ACTION_COLORS[action] ?? "bg-[var(--color-surface)] text-[var(--color-fg-muted)] border border-[var(--color-border)]"
    )}>
      {action.replace(/_/g, " ")}
    </span>
  );
}

function DetailsCell({ details }: { details: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const entries = Object.entries(details);
  if (!entries.length) return null;

  if (!open) {
    const preview = entries.slice(0, 2).map(([k, v]) => `${k}: ${JSON.stringify(v)}`).join(", ");
    return (
      <button onClick={() => setOpen(true)}
        className="text-left text-[var(--color-fg-subtle)] hover:text-[var(--color-fg-muted)] underline decoration-dotted">
        {preview.length > 60 ? preview.slice(0, 60) + "…" : preview}
      </button>
    );
  }
  return (
    <button onClick={() => setOpen(false)} className="text-left">
      <pre className="max-w-xs overflow-x-auto whitespace-pre-wrap text-[0.68rem]">
        {JSON.stringify(details, null, 2)}
      </pre>
    </button>
  );
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return (
      d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) +
      " " +
      d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    );
  } catch { return iso; }
}
