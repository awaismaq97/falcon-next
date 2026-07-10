// Polymarket data-feed proxy — server route handler (Node runtime).
//
// The browser hits this same-origin endpoint instead of Polymarket directly, so
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
// Only public, unauthenticated Polymarket data is read.
//
// Mounted OUTSIDE /api/* on purpose: in production the ingress routes /api/* to
// the FastAPI backend, so this proxy lives at /polymarket/* to reach Next.

import { NextResponse } from "next/server";
import type { PolyFeed, PolyMarket } from "@/lib/polymarket";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Top active, open markets by 24h volume. Params (search/sort/limit) are applied
// to this cached superset per-request, so the cache is a single param-independent
// entry that any request can fall back to.
const UPSTREAM =
  "https://gamma-api.polymarket.com/markets" +
  "?limit=100&active=true&closed=false&archived=false&order=volume24hr&ascending=false";

const FRESH_MS = 5 * 60_000; // serve cache without revalidating (5 min)
const HARD_DEADLINE_MS = 20_000; // absolute cap on a live refresh, across retries
const ATTEMPT_TIMEOUT_MS = 8_000; // per upstream attempt
const MAX_ATTEMPTS = 3;

type Cache = { data: PolyMarket[]; fetchedAt: number };
let CACHE: Cache | null = null;
let INFLIGHT: Promise<PolyMarket[]> | null = null;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function num(v: unknown): number {
  const n = typeof v === "number" ? v : parseFloat(String(v ?? ""));
  return isFinite(n) ? n : 0;
}

/** Gamma returns outcomes / prices / token ids as JSON-encoded strings. */
function parseJsonArray(v: unknown): string[] {
  if (Array.isArray(v)) return v.map(String);
  if (typeof v !== "string") return [];
  try {
    const parsed = JSON.parse(v);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

function normalize(raw: unknown): PolyMarket[] {
  if (!Array.isArray(raw)) return [];
  const out: PolyMarket[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const r = item as Record<string, unknown>;
    const labels = parseJsonArray(r.outcomes);
    const prices = parseJsonArray(r.outcomePrices).map(num);
    if (!r.question || labels.length === 0) continue;

    const events = Array.isArray(r.events) ? (r.events as Record<string, unknown>[]) : [];
    const ev = events[0];
    const eventSlug = (ev?.slug as string) || (r.slug as string) || "";

    out.push({
      id: String(r.id ?? r.conditionId ?? r.slug ?? out.length),
      question: String(r.question),
      slug: String(r.slug ?? ""),
      icon: (r.icon as string) || null,
      image: (r.image as string) || null,
      category: (r.category as string) || (ev?.category as string) || null,
      eventTitle: (ev?.title as string) || null,
      outcomes: labels.map((label, i) => ({ label, price: prices[i] ?? 0 })),
      volume: num(r.volumeNum ?? r.volume),
      volume24hr: num(r.volume24hr),
      liquidity: num(r.liquidityNum ?? r.liquidity),
      spread: r.spread != null ? num(r.spread) : null,
      endDate: (r.endDate as string) || (r.endDateIso as string) || null,
      closed: Boolean(r.closed),
      active: Boolean(r.active),
      url: eventSlug ? `https://polymarket.com/event/${eventSlug}` : "https://polymarket.com",
    });
  }
  return out;
}

async function fetchUpstream(deadline: number): Promise<PolyMarket[]> {
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
function refresh(deadline: number): Promise<PolyMarket[]> {
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

type SortKey = "volume24hr" | "volume" | "liquidity";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const limit = clampInt(url.searchParams.get("limit"), 40, 1, 100);
  const sortParam = url.searchParams.get("sort");
  const sort: SortKey = sortParam === "volume" || sortParam === "liquidity" ? sortParam : "volume24hr";
  const search = (url.searchParams.get("search") ?? "").trim().toLowerCase();

  let data: PolyMarket[];
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
        console.warn("[polymarket] refresh failed, serving stale cache:", (e as Error).message);
        data = CACHE.data;
        fetchedAt = CACHE.fetchedAt;
        source = "cache";
      } else {
        return NextResponse.json(
          { error: `Polymarket feed unavailable: ${(e as Error).message}` },
          { status: 503, headers: { "cache-control": "no-store" } },
        );
      }
    }
  }

  let markets = data;
  if (search) {
    markets = markets.filter(
      (m) =>
        m.question.toLowerCase().includes(search) ||
        (m.eventTitle ?? "").toLowerCase().includes(search) ||
        (m.category ?? "").toLowerCase().includes(search),
    );
  }
  markets = [...markets].sort((a, b) => b[sort] - a[sort]).slice(0, limit);

  const body: PolyFeed = {
    markets,
    fetchedAt: new Date(fetchedAt).toISOString(),
    stale: Date.now() - fetchedAt > FRESH_MS,
    source,
    count: markets.length,
  };
  return NextResponse.json(body, { headers: { "cache-control": "no-store" } });
}
