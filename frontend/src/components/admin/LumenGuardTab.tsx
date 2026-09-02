"use client";

/**
 * LumenGuardTab — is each part of the system working?
 *
 * One row per check: database, auth, watcher, tools, persona, storage,
 * workers. Green means verified working just now, amber means working but
 * something is off, red means not working. Details are one click away and
 * collapsed by default, because the answer to "is it working" should be
 * readable without reading anything.
 */

import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, RefreshCw, TriangleAlert, XCircle } from "lucide-react";
import {
  lumenApi,
  type LumenCheck,
  type LumenHealth,
  type LumenReport,
  type LumenState,
} from "@/lib/adminApi";
import { cn } from "@/lib/utils";

const DOT: Record<LumenHealth, string> = {
  ok: "bg-green-500",
  warn: "bg-amber-500",
  down: "bg-red-500",
};

const FG: Record<LumenHealth, string> = {
  ok: "text-green-600 dark:text-green-400",
  warn: "text-amber-600 dark:text-amber-400",
  down: "text-red-600 dark:text-red-400",
};

const LABEL: Record<LumenHealth, string> = {
  ok: "Working",
  warn: "Needs attention",
  down: "Not working",
};

const TITLE: Record<string, string> = {
  database: "Database",
  auth: "Authentication",
  watcher: "Watcher",
  tools: "Tools",
  persona: "Watcher persona",
  storage: "Document storage",
  backup: "Weekly backup",
  workers: "Background workers",
};

export function LumenGuardTab() {
  const [state, setState] = useState<LumenState | null>(null);
  const [report, setReport] = useState<LumenReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const s = await lumenApi.status();
      setState(s);
      setReport(s.last);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function runCheck() {
    setChecking(true);
    setError(null);
    try {
      setReport(await lumenApi.check());
      setState(await lumenApi.status());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setChecking(false);
    }
  }

  async function toggleMonitor() {
    setError(null);
    try {
      setState(state?.running ? await lumenApi.stop() : await lumenApi.start());
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const overall = report?.overall;

  return (
    <div className="p-4 sm:p-6 space-y-5">
      {/* ── Header ── */}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-[var(--color-fg)]">Lumen Guard</h3>
          <p className="mt-1 text-xs text-[var(--color-fg-muted)] max-w-xl">
            Checks that each part of the system is actually working — not that a flag says so.
            The database check writes and reads back, auth round-trips a real session token,
            and the tool check dispatches a real command.
          </p>
        </div>
        <button
          onClick={load}
          className="shrink-0 rounded-lg p-2 text-[var(--color-fg-muted)] hover:bg-[var(--color-surface)] hover:text-[var(--color-fg)] transition-colors"
          title="Reload last result"
        >
          <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </div>
      )}

      {/* ── Overall ── */}
      <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          {overall ? (
            <>
              <span className={cn("h-2.5 w-2.5 shrink-0 rounded-full", DOT[overall])} />
              <span className={cn("text-sm font-semibold", FG[overall])}>
                {overall === "ok" ? "Everything working" : LABEL[overall]}
              </span>
              <span className="text-xs text-[var(--color-fg-muted)]">
                {report.counts.ok} ok
                {report.counts.warn > 0 && ` · ${report.counts.warn} needs attention`}
                {report.counts.down > 0 && ` · ${report.counts.down} not working`}
              </span>
            </>
          ) : (
            <span className="text-sm text-[var(--color-fg-muted)]">
              {loading ? "Loading…" : "No check has run yet."}
            </span>
          )}

          <div className="ml-auto flex items-center gap-2">
            <span className="text-[0.7rem] font-mono text-[var(--color-fg-subtle)]">
              {report ? `checked ${formatTime(report.checked_at)}` : ""}
            </span>
            <button
              onClick={runCheck}
              disabled={checking}
              className="rounded-lg bg-[var(--color-fg)] px-3 py-1.5 text-xs font-semibold text-[var(--color-bg)] hover:opacity-90 disabled:opacity-50 transition-opacity"
            >
              {checking ? "Checking…" : "Check now"}
            </button>
          </div>
        </div>

        {/* Monitor line */}
        <div className="mt-2.5 flex flex-wrap items-center gap-2 border-t border-[var(--color-border)] pt-2.5 text-xs text-[var(--color-fg-muted)]">
          <span className={cn(
            "h-2 w-2 shrink-0 rounded-full",
            state?.running ? "bg-green-500" : "bg-[var(--color-fg-subtle)]",
          )} />
          {state?.running
            ? <>Re-checking automatically every {state.interval_seconds}s</>
            : <>Automatic checking is {state?.enabled === false ? "disabled" : "stopped"}</>}
          <span className="font-mono text-[0.7rem] text-[var(--color-fg-subtle)]">
            pid {state?.pid ?? "—"}
          </span>
          <button
            onClick={toggleMonitor}
            disabled={state?.enabled === false}
            className="ml-auto rounded-lg border border-[var(--color-border)] px-2.5 py-1 text-[0.7rem] text-[var(--color-fg-muted)] hover:bg-[var(--color-bg)] hover:text-[var(--color-fg)] disabled:opacity-50 transition-colors"
          >
            {state?.running ? "Stop" : "Start"}
          </button>
        </div>
      </div>

      {/* ── Checks ── */}
      {report && (
        <div className="flex flex-col gap-2">
          {report.checks.map((c) => <CheckRow key={c.name} check={c} />)}
        </div>
      )}

      {!report && !loading && (
        <div className="rounded-xl border border-dashed border-[var(--color-border)] py-12 text-center text-sm text-[var(--color-fg-subtle)]">
          Press <span className="font-semibold text-[var(--color-fg-muted)]">Check now</span> to
          test the system.
        </div>
      )}
    </div>
  );
}

function CheckRow({ check }: { check: LumenCheck }) {
  const [open, setOpen] = useState(false);
  const facts = Object.entries(check.facts ?? {});
  const Icon = check.state === "ok" ? CheckCircle2
    : check.state === "warn" ? TriangleAlert
    : XCircle;

  return (
    <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3">
      <div className="flex items-start gap-3">
        <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", FG[check.state])} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="text-sm font-medium text-[var(--color-fg)]">
              {TITLE[check.name] ?? check.name}
            </span>
            <span className={cn("text-[0.7rem] font-medium", FG[check.state])}>
              {LABEL[check.state]}
            </span>
          </div>
          <p className="mt-0.5 text-xs leading-snug text-[var(--color-fg-muted)]">
            {check.message}
          </p>

          {facts.length > 0 && (
            <>
              <button
                onClick={() => setOpen(v => !v)}
                className="mt-1.5 text-[0.7rem] text-[var(--color-fg-subtle)] underline decoration-dotted hover:text-[var(--color-fg-muted)]"
              >
                {open ? "hide details" : "details"}
              </button>
              {open && (
                <dl className="mt-2 grid gap-x-4 gap-y-1 text-[0.72rem] sm:grid-cols-2">
                  {facts.map(([k, v]) => (
                    <div key={k} className="flex min-w-0 gap-2">
                      <dt className="shrink-0 text-[var(--color-fg-subtle)]">
                        {k.replace(/_/g, " ")}
                      </dt>
                      <dd className="min-w-0 flex-1 truncate text-right font-mono text-[var(--color-fg-muted)]">
                        {formatFact(v)}
                      </dd>
                    </div>
                  ))}
                </dl>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function formatFact(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "none";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  try {
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return String(value);
  }
}
