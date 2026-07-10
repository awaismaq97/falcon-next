// Polymarket data feed — shared client types, fetch helper, and formatters.
//
// This is a READ-ONLY, observational mirror of Polymarket's public prediction
// markets (odds / liquidity / event metadata). No wallet, API key, or account
// is involved and no orders are ever placed. The browser talks only to our own
// same-origin proxy at /polymarket/markets (a Next route handler) which owns the
// caching, timeout, retry, and stale-fallback guardrails; it never calls
// Polymarket directly. See app/polymarket/markets/route.ts.

export interface PolyOutcome {
  /** Outcome name, e.g. "Yes" / "No" or a candidate's name. */
  label: string;
  /** Implied probability in [0,1] — Polymarket's price *is* the market's odds. */
  price: number;
}

export interface PolyMarket {
  id: string;
  question: string;
  slug: string;
  icon: string | null;
  image: string | null;
  category: string | null;
  eventTitle: string | null;
  outcomes: PolyOutcome[];
  /** Total traded volume, USD. */
  volume: number;
  /** Trailing 24h volume, USD. */
  volume24hr: number;
  /** Resting order-book liquidity, USD. */
  liquidity: number;
  /** Bid/ask spread (0..1) if reported, else null. */
  spread: number | null;
  /** ISO end/resolution date, if any. */
  endDate: string | null;
  closed: boolean;
  active: boolean;
  /** Canonical polymarket.com event page. */
  url: string;
}

export interface PolyFeed {
  markets: PolyMarket[];
  /** ISO timestamp of when the underlying upstream data was actually fetched. */
  fetchedAt: string;
  /** True when served from the fallback cache because a live refresh failed. */
  stale: boolean;
  /** Whether this response revalidated upstream ("live") or served cache. */
  source: "live" | "cache";
  count: number;
}

export interface PolyFeedParams {
  limit?: number;
  sort?: "volume24hr" | "volume" | "liquidity";
  search?: string;
}

/** Fetch the feed from our same-origin proxy. Never calls Polymarket directly. */
export async function fetchPolyFeed(params: PolyFeedParams = {}): Promise<PolyFeed> {
  const qs = new URLSearchParams();
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.sort) qs.set("sort", params.sort);
  if (params.search) qs.set("search", params.search);

  // Client ceiling sits just above the route's 20s hard cap, so the server gets
  // the chance to answer with stale data rather than the browser aborting first.
  let res: Response;
  try {
    res = await fetch(`/polymarket/markets?${qs.toString()}`, {
      signal: AbortSignal.timeout(25_000),
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "TimeoutError") {
      throw new Error("Polymarket feed timed out.");
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
  return res.json() as Promise<PolyFeed>;
}

// ── Formatters ───────────────────────────────────────────────────────────────

const usdCompact = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 1,
});

/** Compact USD, e.g. 87_000_000 → "$87M". */
export function fmtUsd(n: number | null | undefined): string {
  if (n == null || !isFinite(n) || n <= 0) return "$0";
  return usdCompact.format(n);
}

/** Probability as a percentage, e.g. 0.635 → "63.5%". */
export function fmtPct(p: number): string {
  return `${(p * 100).toFixed(1)}%`;
}

/** Coarse relative time, e.g. "just now" / "4m ago" / "2h ago" / "3d ago". */
export function timeAgo(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (!isFinite(ms) || ms < 0) return "just now";
  const mins = Math.floor(ms / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

/** Human date for a market's resolution, e.g. "Jul 10, 2026". */
export function fmtDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}
