// Kalshi data feed — shared client types, fetch helper, and formatters.
//
// A READ-ONLY, observational mirror of Kalshi's public prediction markets
// (odds / volume / open interest / event metadata). No account, API key, or key
// pair is involved and no orders are ever placed. The browser talks only to our
// own same-origin proxy at /kalshi/markets (a Next route handler) which owns the
// caching, timeout, retry, and stale-fallback guardrails; it never calls Kalshi
// directly. See app/kalshi/markets/route.ts.
//
// Kalshi markets are binary: each market resolves Yes/No, and the Yes price *is*
// the market's implied probability. We surface the market question plus the Yes
// contract's meaning (its sub-title), rendered as Yes / No probability bars.

import { fmtPct, timeAgo, fmtDate } from "@/lib/polymarket";

// Re-export the shared display helpers so the Kalshi tab can import everything
// prediction-market formatting-related from one place.
export { fmtPct, timeAgo, fmtDate };

export interface KalshiOutcome {
  /** "Yes" / "No". */
  label: string;
  /** Implied probability in [0,1]. */
  price: number;
}

export interface KalshiMarket {
  id: string;
  /** Kalshi market ticker, e.g. "KXWNBA1HWINNER-26JUL12CHIDAL-D". */
  ticker: string;
  /** The market question, e.g. "Chicago vs Dallas: First Half Winner?". */
  question: string;
  /** What a "Yes" contract means, e.g. "Dallas wins 1st half". */
  yesLabel: string;
  /** Series ticker (prefix of the event), used for grouping / linking. */
  series: string;
  eventTicker: string | null;
  /** Yes / No, sorted so the more likely outcome is first. */
  outcomes: KalshiOutcome[];
  /** Total traded volume, in contracts (~$1 notional each). */
  volume: number;
  /** Trailing 24h volume, in contracts. */
  volume24h: number;
  /** Open interest, in contracts. */
  openInterest: number;
  /** Resting order-book liquidity, USD. */
  liquidity: number;
  /** ISO close/resolution date, if any. */
  closeDate: string | null;
  status: string;
  /** Canonical kalshi.com page for the series. */
  url: string;
}

export interface KalshiFeed {
  markets: KalshiMarket[];
  /** ISO timestamp of when the underlying upstream data was actually fetched. */
  fetchedAt: string;
  /** True when served from the fallback cache because a live refresh failed. */
  stale: boolean;
  /** Whether this response revalidated upstream ("live") or served cache. */
  source: "live" | "cache";
  count: number;
}

export interface KalshiFeedParams {
  limit?: number;
  sort?: "volume24h" | "volume" | "openInterest";
  search?: string;
}

/** Fetch the feed from our same-origin proxy. Never calls Kalshi directly. */
export async function fetchKalshiFeed(params: KalshiFeedParams = {}): Promise<KalshiFeed> {
  const qs = new URLSearchParams();
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.sort) qs.set("sort", params.sort);
  if (params.search) qs.set("search", params.search);

  // Client ceiling sits just above the route's 20s hard cap, so the server gets
  // the chance to answer with stale data rather than the browser aborting first.
  let res: Response;
  try {
    res = await fetch(`/kalshi/markets?${qs.toString()}`, {
      signal: AbortSignal.timeout(25_000),
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "TimeoutError") {
      throw new Error("Kalshi feed timed out.");
    }
    throw new Error(`Network error: ${(e as Error).message}`);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).error ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<KalshiFeed>;
}

// ── Formatters ───────────────────────────────────────────────────────────────

const numCompact = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
});

/** Compact count, e.g. 81_149 → "81.1K". Used for contract volume / OI. */
export function fmtNum(n: number | null | undefined): string {
  if (n == null || !isFinite(n) || n <= 0) return "0";
  return numCompact.format(n);
}
