"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Search,
  RefreshCw,
  ExternalLink,
  TrendingUp,
  AlertTriangle,
  Layers,
  BarChart3,
  Clock,
  ShieldCheck,
} from "lucide-react";
import { useKalshiMarkets } from "@/lib/queries";
import { fmtNum, fmtPct, fmtDate, timeAgo, type KalshiMarket } from "@/lib/kalshi";
import { Button, Badge, Spinner, Card, Input } from "@/components/ui/primitives";
import { Select } from "@/components/ui/controls";
import { cn } from "@/lib/utils";

const OPTIN_KEY = "falcon-kalshi-optin";
const PAGE = 24;

type SortKey = "volume24h" | "volume" | "openInterest";
const SORTS: { value: SortKey; label: string }[] = [
  { value: "volume24h", label: "24h Volume" },
  { value: "volume", label: "Total Volume" },
  { value: "openInterest", label: "Open Interest" },
];

export function KalshiTab() {
  // "Clear opt-in for external sync" — the feed stays dark until the user enables
  // it. Read the persisted flag in an effect so SSR and first render agree.
  const [optedIn, setOptedIn] = useState(false);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    setOptedIn(localStorage.getItem(OPTIN_KEY) === "1");
    setReady(true);
  }, []);
  function enable() {
    localStorage.setItem(OPTIN_KEY, "1");
    setOptedIn(true);
  }

  const { data, isLoading, isFetching, isError, error, refetch } = useKalshiMarkets(optedIn);

  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortKey>("volume24h");
  const [shown, setShown] = useState(PAGE);

  const all = useMemo(() => data?.markets ?? [], [data]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const list = q
      ? all.filter(
          (m) =>
            m.question.toLowerCase().includes(q) ||
            m.yesLabel.toLowerCase().includes(q) ||
            m.series.toLowerCase().includes(q),
        )
      : all;
    return [...list].sort((a, b) => b[sort] - a[sort]);
  }, [all, search, sort]);

  const totals = useMemo(
    () => ({
      count: all.length,
      vol24: all.reduce((s, m) => s + m.volume24h, 0),
      oi: all.reduce((s, m) => s + m.openInterest, 0),
    }),
    [all],
  );

  // Reset paging whenever the filter/sort changes.
  useEffect(() => setShown(PAGE), [search, sort]);

  if (!ready) return null;
  if (!optedIn) return <OptInGate onEnable={enable} />;

  const visible = filtered.slice(0, shown);

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="shrink-0 border-b border-[var(--color-border)] px-5 py-4">
        <div className="mx-auto flex max-w-5xl items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-[1rem] font-semibold">
              <Layers className="h-4 w-4" /> Kalshi
            </h2>
            <p className="text-[0.78rem] text-[var(--color-fg-subtle)]">
              Observational mirror of Kalshi&rsquo;s public prediction markets — collective
              expectations, not trading advice.
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {data && (
              <span className="hidden items-center gap-1 text-[0.72rem] text-[var(--color-fg-subtle)] sm:flex">
                <Clock className="h-3 w-3" /> {timeAgo(data.fetchedAt)}
              </span>
            )}
            <Button size="sm" variant="secondary" onClick={() => refetch()} loading={isFetching}>
              <RefreshCw className="h-3.5 w-3.5" /> Refresh
            </Button>
          </div>
        </div>
      </div>

      {/* Body */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl px-5 py-4">
          {/* Stale fallback banner */}
          {data?.stale && (
            <div className="mb-3 flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[0.78rem] text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              Live refresh unavailable — showing cached data from {timeAgo(data.fetchedAt)}.
            </div>
          )}

          {/* Controls */}
          <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center">
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--color-fg-subtle)]" />
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search markets…"
                className="pl-9"
              />
            </div>
            <div className="w-full sm:w-48">
              <Select value={sort} onValueChange={(v) => setSort(v as SortKey)} options={SORTS} />
            </div>
          </div>

          {/* Aggregate stats */}
          <div className="mb-4 grid grid-cols-3 gap-3">
            <Stat icon={<BarChart3 className="h-4 w-4" />} label="Markets" value={String(totals.count)} />
            <Stat icon={<TrendingUp className="h-4 w-4" />} label="24h Volume" value={fmtNum(totals.vol24)} />
            <Stat icon={<Layers className="h-4 w-4" />} label="Open Interest" value={fmtNum(totals.oi)} />
          </div>

          {/* Feed */}
          {isLoading ? (
            <div className="flex items-center gap-2 py-10 text-[var(--color-fg-subtle)]">
              <Spinner /> Loading markets…
            </div>
          ) : isError ? (
            <div className="flex flex-col items-center gap-2 py-10 text-center">
              <AlertTriangle className="h-6 w-6 text-[var(--color-red)]" />
              <div className="text-[0.85rem] text-[var(--color-fg-muted)]">
                {(error as Error)?.message ?? "Could not load the feed."}
              </div>
              <Button size="sm" variant="secondary" onClick={() => refetch()}>
                <RefreshCw className="h-3.5 w-3.5" /> Try again
              </Button>
            </div>
          ) : filtered.length === 0 ? (
            <p className="py-10 text-center text-[0.85rem] text-[var(--color-fg-subtle)]">
              No markets match &ldquo;{search}&rdquo;.
            </p>
          ) : (
            <>
              <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                {visible.map((m) => (
                  <MarketCard key={m.id} m={m} />
                ))}
              </div>
              {shown < filtered.length && (
                <div className="py-5 text-center">
                  <Button size="sm" variant="secondary" onClick={() => setShown((n) => n + PAGE)}>
                    Show more ({filtered.length - shown})
                  </Button>
                </div>
              )}
            </>
          )}

          {/* Policy footer */}
          <p className="mt-4 flex items-center justify-center gap-1.5 text-center text-[0.7rem] text-[var(--color-fg-subtle)]">
            <ShieldCheck className="h-3 w-3" />
            Read-only public data · cached ~5 min · not persisted · observational only
          </p>
        </div>
      </div>
    </div>
  );
}

function Stat({ icon, label, value }: { icon: ReactNode; label: string; value: string }) {
  return (
    <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2.5">
      <div className="flex items-center gap-1.5 text-[0.7rem] text-[var(--color-fg-subtle)]">
        {icon} {label}
      </div>
      <div className="mt-0.5 font-mono text-[1.05rem] font-semibold tabular-nums text-[var(--color-fg)]">
        {value}
      </div>
    </div>
  );
}

function MarketCard({ m }: { m: KalshiMarket }) {
  const leadPrice = m.outcomes[0]?.price ?? 0;

  return (
    <div className="flex flex-col rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4 transition-colors hover:border-[var(--color-border-strong)]">
      {/* Header */}
      <div className="flex items-start gap-3">
        <a href={m.url} target="_blank" rel="noreferrer noopener" className="group min-w-0 flex-1">
          <span className="line-clamp-2 text-[0.9rem] font-medium leading-snug text-[var(--color-fg)] group-hover:underline">
            {m.question}
          </span>
        </a>
        <ExternalLink className="h-3.5 w-3.5 shrink-0 text-[var(--color-fg-subtle)]" />
      </div>

      {/* Meta — the Yes contract's meaning, plus the series tag */}
      {(m.yesLabel && m.yesLabel !== m.question) && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <Badge color="gray">{m.series}</Badge>
          <span className="min-w-0 truncate text-[0.72rem] text-[var(--color-fg-subtle)]" title={m.yesLabel}>
            Yes = {m.yesLabel}
          </span>
        </div>
      )}

      {/* Outcomes — Yes / No */}
      <div className="mt-3 space-y-1.5">
        {m.outcomes.map((o, i) => (
          <OutcomeBar key={o.label + i} label={o.label} price={o.price} lead={o.price === leadPrice} />
        ))}
      </div>

      {/* Footer stats */}
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-[var(--color-border)] pt-2.5 text-[0.72rem] text-[var(--color-fg-subtle)]">
        <span className="flex items-center gap-1">
          <TrendingUp className="h-3 w-3" /> {fmtNum(m.volume24h)} 24h
        </span>
        <span className="flex items-center gap-1">
          <Layers className="h-3 w-3" /> {fmtNum(m.openInterest)} OI
        </span>
        {m.closeDate && (
          <span className="ml-auto flex items-center gap-1">
            <Clock className="h-3 w-3" /> {fmtDate(m.closeDate)}
          </span>
        )}
      </div>
    </div>
  );
}

function OutcomeBar({ label, price, lead }: { label: string; price: number; lead: boolean }) {
  const pct = Math.max(0, Math.min(100, price * 100));
  return (
    <div className="flex items-center gap-2">
      <span className="w-20 shrink-0 truncate text-[0.78rem] text-[var(--color-fg-muted)]" title={label}>
        {label}
      </span>
      <div className="relative h-2 flex-1 overflow-hidden rounded-full bg-[var(--color-surface-2)]">
        <div
          className={cn(
            "absolute inset-y-0 left-0 rounded-full",
            lead ? "bg-[var(--color-fg)]" : "bg-[var(--color-border-strong)]",
          )}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="w-12 shrink-0 text-right font-mono text-[0.78rem] tabular-nums text-[var(--color-fg)]">
        {fmtPct(price)}
      </span>
    </div>
  );
}

function OptInGate({ onEnable }: { onEnable: () => void }) {
  return (
    <div className="flex h-full items-center justify-center p-6">
      <Card className="max-w-md">
        <div className="flex items-center gap-2 text-[1rem] font-semibold">
          <Layers className="h-5 w-5" /> Kalshi feed
        </div>
        <p className="mt-2 text-[0.85rem] leading-relaxed text-[var(--color-fg-muted)]">
          A read-only, observational mirror of{" "}
          <a href="https://kalshi.com" target="_blank" rel="noreferrer noopener" className="underline">
            Kalshi
          </a>
          &rsquo;s public prediction markets — Yes/No odds, volume, and open interest as a snapshot of
          collective expectations.
        </p>
        <ul className="mt-3 space-y-1.5 text-[0.78rem] text-[var(--color-fg-subtle)]">
          <li className="flex gap-2">
            <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" /> No account, key pair, or trading —
            display only.
          </li>
          <li className="flex gap-2">
            <RefreshCw className="mt-0.5 h-4 w-4 shrink-0" /> Public data, cached ~5 min; nothing stored
            long-term.
          </li>
          <li className="flex gap-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> Not financial advice.
          </li>
        </ul>
        <p className="mt-3 text-[0.75rem] text-[var(--color-fg-subtle)]">
          Enabling fetches data from Kalshi&rsquo;s public API.
        </p>
        <Button className="mt-4 w-full justify-center" variant="primary" onClick={onEnable}>
          Enable live feed
        </Button>
      </Card>
    </div>
  );
}
