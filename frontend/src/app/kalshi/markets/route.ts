// Kalshi data-feed proxy — server route handler (Node runtime).
//
// The browser hits this same-origin endpoint instead of Kalshi directly, so
// every guardrail lives here:
//   • Async, non-blocking upstream fetch.
//   • Hard 20s total response cap (across retries) — the request never hangs.
//   • Retry with short backoff on transient upstream failures.
//   • 5-minute in-memory freshness window → upstream is hit at most ~once / 5 min
//     regardless of inbound traffic (our upstream rate limit), and concurrent
//     refreshes are coalesced into a single upstream call.
//   • Fallback protocol: if a live refresh fails or times out, we serve the last
//     good cached snapshot (flagged `stale`) and log the drop, instead of erroring.
//
// Data policy: raw feeds are NOT persisted. The only copy is this ephemeral,
// in-process cache — overwritten on every refresh and gone on restart — so it is
// inherently within the 72h purge window with nothing stored at rest to encrypt.
// Only public, unauthenticated Kalshi data is read.
//
// Mounted OUTSIDE /api/* on purpose: in production the ingress routes /api/* to
// the FastAPI backend, so this proxy lives at /kalshi/* to reach Next.

import { NextResponse } from "next/server";
import type { KalshiFeed, KalshiMarket } from "@/lib/kalshi";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Kalshi's markets endpoint has no volume sort, so we pull a large superset of
// open markets and rank/slice per-request. `mve_filter=exclude` drops the noisy
// auto-generated multivariate combo markets (ugly concatenated titles, no
// standalone meaning). The cache is a single param-independent entry that any
// request can fall back to.
const UPSTREAM =
  "https://api.elections.kalshi.com/trade-api/v2/markets" +
  "?limit=1000&status=open&mve_filter=exclude";

const FRESH_MS = 5 * 60_000; // serve cache without revalidating (5 min)
const HARD_DEADLINE_MS = 20_000; // absolute cap on a live refresh, across retries
const ATTEMPT_TIMEOUT_MS = 8_000; // per upstream attempt
const MAX_ATTEMPTS = 3;

type Cache = { data: KalshiMarket[]; fetchedAt: number };
let CACHE: Cache | null = null;
let INFLIGHT: Promise<KalshiMarket[]> | null = null;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function num(v: unknown): number {
  const n = typeof v === "number" ? v : parseFloat(String(v ?? ""));
  return isFinite(n) ? n : 0;
}

/** Series ticker is the segment of the event ticker before the first dash. */
function seriesOf(eventTicker: string): string {
  const dash = eventTicker.indexOf("-");
  return dash > 0 ? eventTicker.slice(0, dash) : eventTicker;
}

/** Yes implied probability: mid of the bid/ask when both quote, else last trade. */
function yesProbability(r: Record<string, unknown>): number {
  const bid = num(r.yes_bid_dollars);
  const ask = num(r.yes_ask_dollars);
  if (bid > 0 && ask > 0) return (bid + ask) / 2;
  const last = num(r.last_price_dollars);
  if (last > 0) return last;
  return bid || ask;
}

function normalize(raw: unknown): KalshiMarket[] {
  const markets = (raw as { markets?: unknown })?.markets;
  if (!Array.isArray(markets)) return [];
  const out: KalshiMarket[] = [];
  for (const item of markets) {
    if (!item || typeof item !== "object") continue;
    const r = item as Record<string, unknown>;
    if (!r.ticker || !r.title) continue;

    const yes = Math.max(0, Math.min(1, yesProbability(r)));
    const eventTicker = String(r.event_ticker ?? "");
    const series = seriesOf(eventTicker || String(r.ticker));

    out.push({
      id: String(r.ticker),
      ticker: String(r.ticker),
      question: String(r.title),
      yesLabel: String(r.yes_sub_title ?? "Yes"),
      series,
      eventTicker: eventTicker || null,
      outcomes: [
        { label: "Yes", price: yes },
        { label: "No", price: 1 - yes },
      ].sort((a, b) => b.price - a.price),
      volume: num(r.volume_fp),
      volume24h: num(r.volume_24h_fp),
      openInterest: num(r.open_interest_fp),
      liquidity: num(r.liquidity_dollars),
      closeDate: (r.close_time as string) || null,
      status: String(r.status ?? ""),
      // Kalshi's web routes use the upper-case series ticker verbatim, e.g.
      // https://kalshi.com/markets/KXHIGHNY — do NOT lower-case it (that 404s).
      url: series ? `https://kalshi.com/markets/${series}` : "https://kalshi.com/markets",
    });
  }
  return out;
}

async function fetchUpstream(deadline: number): Promise<KalshiMarket[]> {
  let lastErr: unknown = new Error("upstream unavailable");
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const remaining = deadline - Date.now();
    if (remaining <= 0) break;
    try {
      const res = await fetch(UPSTREAM, {
        signal: AbortSignal.timeout(Math.min(ATTEMPT_TIMEOUT_MS, remaining)),
        headers: { accept: "application/json" },
        cache: "no-store", // we manage our own cache
      });
      if (!res.ok) throw new Error(`upstream ${res.status}`);
      const data = normalize(await res.json());
      if (data.length === 0) throw new Error("empty upstream payload");
      return data;
    } catch (e) {
      lastErr = e;
      const backoff = Math.min(500 * attempt, deadline - Date.now());
      if (attempt < MAX_ATTEMPTS && backoff > 0) await sleep(backoff);
    }
  }
  throw lastErr;
}

/** Refresh the cache, coalescing concurrent callers into one upstream call. */
function refresh(deadline: number): Promise<KalshiMarket[]> {
  if (!INFLIGHT) {
    INFLIGHT = fetchUpstream(deadline)
      .then((data) => {
        CACHE = { data, fetchedAt: Date.now() };
        return data;
      })
      .finally(() => {
        INFLIGHT = null;
      });
  }
  return INFLIGHT;
}

function clampInt(v: string | null, dflt: number, min: number, max: number): number {
  const n = parseInt(v ?? "", 10);
  if (!isFinite(n)) return dflt;
  return Math.max(min, Math.min(max, n));
}

type SortKey = "volume24h" | "volume" | "openInterest";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const limit = clampInt(url.searchParams.get("limit"), 40, 1, 200);
  const sortParam = url.searchParams.get("sort");
  const sort: SortKey =
    sortParam === "volume" || sortParam === "openInterest" ? sortParam : "volume24h";
  const search = (url.searchParams.get("search") ?? "").trim().toLowerCase();

  let data: KalshiMarket[];
  let fetchedAt: number;
  let source: "live" | "cache";

  if (CACHE && Date.now() - CACHE.fetchedAt < FRESH_MS) {
    // Within the freshness window — serve cache, no upstream hit.
    data = CACHE.data;
    fetchedAt = CACHE.fetchedAt;
    source = "cache";
  } else {
    try {
      data = await refresh(Date.now() + HARD_DEADLINE_MS);
      fetchedAt = CACHE!.fetchedAt;
      source = "live";
    } catch (e) {
      if (CACHE) {
        // Fallback protocol: serve the last good snapshot, flag it stale.
        console.warn("[kalshi] refresh failed, serving stale cache:", (e as Error).message);
        data = CACHE.data;
        fetchedAt = CACHE.fetchedAt;
        source = "cache";
      } else {
        return NextResponse.json(
          { error: `Kalshi feed unavailable: ${(e as Error).message}` },
          { status: 503, headers: { "cache-control": "no-store" } },
        );
      }
    }
  }

  // Only surface markets that have actually traded — the open set is dominated
  // by freshly-listed zero-volume markets that carry no signal.
  let markets = data.filter((m) => m.volume24h > 0 || m.volume > 0);
  if (search) {
    markets = markets.filter(
      (m) =>
        m.question.toLowerCase().includes(search) ||
        m.yesLabel.toLowerCase().includes(search) ||
        m.series.toLowerCase().includes(search),
    );
  }
  markets = [...markets].sort((a, b) => b[sort] - a[sort]).slice(0, limit);

  const body: KalshiFeed = {
    markets,
    fetchedAt: new Date(fetchedAt).toISOString(),
    stale: Date.now() - fetchedAt > FRESH_MS,
    source,
    count: markets.length,
  };
  return NextResponse.json(body, { headers: { "cache-control": "no-store" } });
}
