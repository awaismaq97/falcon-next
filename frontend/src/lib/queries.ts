"use client";

import { useCallback, useEffect, useRef } from "react";
import { useQuery, useQueryClient, useInfiniteQuery } from "@tanstack/react-query";
import { api, API_BASE } from "./api";
import { fetchPolyFeed } from "./polymarket";
import { fetchKalshiFeed } from "./kalshi";
import type { Category, MemoryType, Message } from "./types";

type HistoryData = { identity_id: string; messages: Message[]; count: number };

export const qk = {
  config: ["config"] as const,
  identities: ["identities"] as const,
  history: (id: string) => ["history", id] as const,
  tokens: (id: string) => ["tokens", id] as const,
  systemPrompt: (id: string) => ["system-prompt", id] as const,
  memory: (id: string, type?: MemoryType) => ["memory", id, type ?? "all"] as const,
  persona: (id: string) => ["persona", id] as const,
  personas: (id: string) => ["personas", id] as const,
  traceIndex: (id: string) => ["trace-index", id] as const,
  latestContext: (id: string) => ["latest-context", id] as const,
  traces: (id: string) => ["traces", id] as const,
  audit: (id: string) => ["audit", id] as const,
  dualRun: (id: string) => ["dual-run", id] as const,
  testingRegistry: ["testing-registry"] as const,
  testingHistory: (slug: string) => ["testing-history", slug] as const,
  testingReport: (slug: string) => ["testing-report", slug] as const,
  voiceConfig: ["voice-config"] as const,
  polymarket: (limit: number) => ["polymarket", limit] as const,
  kalshi: (limit: number) => ["kalshi", limit] as const,
  categories: (id: string) => ["categories", id] as const,
  categoryMessages: (id: string, categoryId: string) => ["category-messages", id, categoryId] as const,
  watcherStatus: (id: string) => ["watcher-status", id] as const,
  watcherLog: (id: string) => ["watcher-log", id] as const,
  watcherAgents: () => ["watcher-agents"] as const,
};

export const useConfig = () => useQuery({ queryKey: qk.config, queryFn: api.getConfig, staleTime: Infinity });

// Voice catalog (ElevenLabs). Cached a few minutes; one retry so a transient
// upstream blip doesn't permanently disable the controls.
export const useVoiceConfig = () =>
  useQuery({ queryKey: qk.voiceConfig, queryFn: api.getVoiceConfig, staleTime: 5 * 60 * 1000, retry: 1 });

export const useIdentities = () =>
  useQuery({ queryKey: qk.identities, queryFn: api.listIdentities });

export const useHistory = (id: string) =>
  useQuery({ queryKey: qk.history(id), queryFn: () => api.loadHistory(id), enabled: !!id });

export const useTokens = (id: string) =>
  useQuery({ queryKey: qk.tokens(id), queryFn: () => api.getTokens(id), enabled: !!id });

// Per-identity system prompt. staleTime 0 (default) so switching identity
// refetches; the sidebar hydrates the settings store from this.
export const useSystemPrompt = (id: string) =>
  useQuery({ queryKey: qk.systemPrompt(id), queryFn: () => api.getSystemPrompt(id), enabled: !!id });

export const useMemory = (id: string, type?: MemoryType) =>
  useQuery({ queryKey: qk.memory(id, type), queryFn: () => api.listMemory(id, type), enabled: !!id });

export const usePersona = (id: string) =>
  useQuery({ queryKey: qk.persona(id), queryFn: () => api.getPersona(id), enabled: !!id });

export const usePersonas = (id: string) =>
  useQuery({ queryKey: qk.personas(id), queryFn: () => api.listPersonas(id), enabled: !!id });

export const useTraceIndex = (id: string) =>
  useQuery({ queryKey: qk.traceIndex(id), queryFn: () => api.traceIndex(id), enabled: !!id });

export const useLatestContext = (id: string) =>
  useQuery({ queryKey: qk.latestContext(id), queryFn: () => api.latestContext(id), enabled: !!id });

const PAGE_SIZE = 25;

export const useTraces = (id: string) =>
  useInfiniteQuery({
    queryKey: qk.traces(id),
    queryFn: ({ pageParam = 0 }) => api.listTraces(id, pageParam as number, PAGE_SIZE),
    initialPageParam: 0,
    getNextPageParam: (lastPage) => {
      const fetched = lastPage.skip + lastPage.traces.length;
      return fetched < lastPage.total ? fetched : undefined;
    },
    enabled: !!id,
  });

export const useAuditSummaries = (id: string) =>
  useInfiniteQuery({
    queryKey: qk.audit(id),
    queryFn: ({ pageParam = 0 }) => api.auditSummaries(id, pageParam as number, PAGE_SIZE),
    initialPageParam: 0,
    getNextPageParam: (lastPage) => {
      const fetched = lastPage.skip + lastPage.records.length;
      return fetched < lastPage.total ? fetched : undefined;
    },
    enabled: !!id,
  });

export const useDualRuns = (id: string) =>
  useQuery({ queryKey: qk.dualRun(id), queryFn: () => api.dualRuns(id), enabled: !!id });

export const useTestingRegistry = () =>
  useQuery({ queryKey: qk.testingRegistry, queryFn: api.testingRegistry });

export const useTestingHistory = (slug: string) =>
  useQuery({ queryKey: qk.testingHistory(slug), queryFn: () => api.testingHistory(slug), enabled: !!slug });

export const useTestingReport = (slug: string) =>
  useQuery({ queryKey: qk.testingReport(slug), queryFn: () => api.testingReport(slug), enabled: !!slug });

// Polymarket public feed. Mirrors the server proxy's 5-min cache and auto-
// refreshes on the same cadence while the tab is mounted; one retry smooths a
// transient blip. Gated by `enabled` so nothing is fetched until the user opts in.
export const usePolyMarkets = (enabled = true, limit = 100) =>
  useQuery({
    queryKey: qk.polymarket(limit),
    queryFn: () => fetchPolyFeed({ limit }),
    enabled,
    staleTime: 5 * 60 * 1000,
    refetchInterval: enabled ? 5 * 60 * 1000 : false,
    retry: 1,
  });

// Kalshi public feed. Mirrors the server proxy's 5-min cache and auto-refreshes
// on the same cadence while the tab is mounted; one retry smooths a transient
// blip. Gated by `enabled` so nothing is fetched until the user opts in.
export const useKalshiMarkets = (enabled = true, limit = 200) =>
  useQuery({
    queryKey: qk.kalshi(limit),
    queryFn: () => fetchKalshiFeed({ limit }),
    enabled,
    staleTime: 5 * 60 * 1000,
    refetchInterval: enabled ? 5 * 60 * 1000 : false,
    retry: 1,
  });

export const useCategories = (id: string) =>
  useQuery({
    queryKey: qk.categories(id),
    queryFn: () => api.listCategories(id),
    enabled: !!id,
    staleTime: 30 * 1000,
  });

export const useCategoryMessages = (id: string, categoryId: string, skip = 0, limit = 20) =>
  useQuery({
    queryKey: [...qk.categoryMessages(id, categoryId), skip, limit],
    queryFn: () => api.listCategoryMessages(id, categoryId, skip, limit),
    enabled: !!id && !!categoryId,
    staleTime: 10 * 1000,
  });

export const useWatcherStatus = (id: string) =>
  useQuery({
    queryKey: qk.watcherStatus(id),
    queryFn: () => api.watcherStatus(id),
    enabled: !!id,
    refetchInterval: 5_000,
    staleTime: 0,  // always refetch when invalidated
  });

export const useWatcherLog = (id: string, limit = 50) =>
  useQuery({
    queryKey: qk.watcherLog(id),
    queryFn: () => api.watcherLog(id, limit),
    enabled: !!id,
    staleTime: 10_000,
    refetchInterval: 10_000,
  });

/** Registered agents. Only fetched while the manager dialog is open. */
export const useWatcherAgents = (enabled = true) =>
  useQuery({
    queryKey: qk.watcherAgents(),
    queryFn: () => api.watcherAgents(),
    enabled,
    staleTime: 15_000,
  });

/**
 * Subscribes to the watcher SSE stream for instant result delivery.
 * When a watcher_result event arrives, appends it directly to the
 * history cache — no polling, no delay.
 *
 * `busyRef` guards message ordering. The in-flight turn lives in ChatTab's
 * `pending` state, which renders after everything in the history cache, so a
 * result appended while the turn is still streaming would jump above the
 * message that triggered it. A fast watcher answers well within the turn, so
 * that is the normal case, not an edge case. While busy we skip the optimistic
 * append and record that something was missed; the caller invokes the returned
 * function once the turn has settled and refetches instead, letting the server's
 * insertion order settle the sequence.
 *
 * Returns: () => boolean — true when results arrived mid-turn and the caller
 * should refetch history rather than trust the appended cache.
 */
export function useWatcherResultPoller(
  id: string,
  busyRef?: { current: boolean },
) {
  const qc = useQueryClient();
  const missedRef = useRef(false);

  useEffect(() => {
    if (!id) return;

    const token = typeof window !== "undefined"
      ? localStorage.getItem("falcon-auth-token")
      : null;

    const url = `${API_BASE}/api/identities/${encodeURIComponent(id)}/watcher/stream`;
    const es = new EventSource(
      token ? `${url}?token=${encodeURIComponent(token)}` : url
    );

    es.addEventListener("watcher_result", (e: MessageEvent) => {
      try {
        const msg = JSON.parse(e.data);

        // Mid-turn: appending now would render the result above the message it
        // answers. Defer to a refetch once the turn has settled.
        if (busyRef?.current) {
          missedRef.current = true;
          qc.invalidateQueries({ queryKey: qk.watcherLog(id) });
          return;
        }

        // Append the injected result directly into the history cache.
        qc.setQueryData<{ identity_id: string; messages: unknown[]; count: number }>(
          qk.history(id),
          (old) => {
            if (!old) return old;
            const newMsg = {
              role: "assistant",
              content: msg.content ?? "",
              timestamp: msg.timestamp ?? "",
              _watcher: true,
            };
            return {
              ...old,
              messages: [...old.messages, newMsg],
              count: old.count + 1,
            };
          }
        );
        // Also refresh the watcher log panel.
        qc.invalidateQueries({ queryKey: qk.watcherLog(id) });
      } catch {
        // malformed event — fallback to full refetch
        qc.invalidateQueries({ queryKey: qk.history(id) });
      }
    });

    es.onerror = () => {
      // On connection error fall back to a single refetch so nothing is lost.
      qc.invalidateQueries({ queryKey: qk.history(id) });
    };

    return () => es.close();
  }, [id, qc, busyRef]);

  // Consumed by ChatTab when a turn finishes. Clears the flag and reports
  // whether history needs a refetch to restore the true order.
  return useCallback(() => {
    if (!missedRef.current) return false;
    missedRef.current = false;
    return true;
  }, []);
}

/** Convenience: invalidate everything scoped to one identity after a turn/edit.
 *  Pass `{ includeHistory: false }` when the history cache was already updated
 *  in place (see useHistoryAppender) so we don't refetch the whole tail. */
export function useIdentityInvalidator() {
  const qc = useQueryClient();
  return (id: string, opts?: { includeHistory?: boolean }) => {
    if (opts?.includeHistory !== false) qc.invalidateQueries({ queryKey: qk.history(id) });
    qc.invalidateQueries({ queryKey: qk.tokens(id) });
    qc.invalidateQueries({ queryKey: ["memory", id] });
    qc.invalidateQueries({ queryKey: qk.traceIndex(id) });
    qc.invalidateQueries({ queryKey: qk.latestContext(id) });
    qc.invalidateQueries({ queryKey: qk.traces(id) });
    qc.invalidateQueries({ queryKey: qk.audit(id) });
    qc.invalidateQueries({ queryKey: qk.dualRun(id) });
    qc.invalidateQueries({ queryKey: qk.identities });
  };
}

/** Newest conversation turns retained per identity. Mirrors
 *  CONVERSATION_RETENTION_TURNS in the backend (falcon/logger.py). A turn is a
 *  user message plus its assistant response, so the tail is trimmed by user
 *  message, not by raw message count. */
export const CONVERSATION_RETENTION_TURNS = 15;

/** Trim `messages` to the newest `turns` turns: keep everything from the
 *  `turns`-th newest user message onward, mirroring the server's prune cutoff. */
function tailByTurns(messages: Message[], turns: number): Message[] {
  let seen = 0;
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === "user") {
      seen += 1;
      if (seen === turns) return messages.slice(i);
    }
  }
  return messages; // fewer than `turns` turns present — keep all
}

/** Append confirmed messages to the cached history in place — avoids refetching
 *  the entire message tail after every single turn. The cache is trimmed to the
 *  newest CONVERSATION_RETENTION_TURNS turns so it matches what the server has
 *  retained. Returns false if history isn't cached yet, so the caller can fall
 *  back to a refetch. */
export function useHistoryAppender() {
  const qc = useQueryClient();
  return (id: string, messages: Message[]): boolean => {
    const key = qk.history(id);
    const existing = qc.getQueryData<HistoryData>(key);
    if (!existing) return false;
    const merged = [...existing.messages, ...messages];
    const trimmed = tailByTurns(merged, CONVERSATION_RETENTION_TURNS);
    qc.setQueryData<HistoryData>(key, {
      ...existing,
      messages: trimmed,
      count: trimmed.length,
    });
    return true;
  };
}
