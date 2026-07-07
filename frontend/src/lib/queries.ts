"use client";

import { useQuery, useQueryClient, useInfiniteQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { MemoryType, Message } from "./types";

type HistoryData = { identity_id: string; messages: Message[]; count: number };

export const qk = {
  config: ["config"] as const,
  identities: ["identities"] as const,
  history: (id: string) => ["history", id] as const,
  tokens: (id: string) => ["tokens", id] as const,
  memory: (id: string, type?: MemoryType) => ["memory", id, type ?? "all"] as const,
  persona: (id: string) => ["persona", id] as const,
  traceIndex: (id: string) => ["trace-index", id] as const,
  latestContext: (id: string) => ["latest-context", id] as const,
  traces: (id: string) => ["traces", id] as const,
  audit: (id: string) => ["audit", id] as const,
  dualRun: (id: string) => ["dual-run", id] as const,
  testingRegistry: ["testing-registry"] as const,
  testingHistory: (slug: string) => ["testing-history", slug] as const,
  testingReport: (slug: string) => ["testing-report", slug] as const,
  voiceConfig: ["voice-config"] as const,
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

export const useMemory = (id: string, type?: MemoryType) =>
  useQuery({ queryKey: qk.memory(id, type), queryFn: () => api.listMemory(id, type), enabled: !!id });

export const usePersona = (id: string) =>
  useQuery({ queryKey: qk.persona(id), queryFn: () => api.getPersona(id), enabled: !!id });

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

/** Append confirmed messages to the cached history in place — avoids refetching
 *  the entire (up to thousands) message tail after every single turn. Returns
 *  false if history isn't cached yet, so the caller can fall back to a refetch. */
export function useHistoryAppender() {
  const qc = useQueryClient();
  return (id: string, messages: Message[]): boolean => {
    const key = qk.history(id);
    const existing = qc.getQueryData<HistoryData>(key);
    if (!existing) return false;
    qc.setQueryData<HistoryData>(key, {
      ...existing,
      messages: [...existing.messages, ...messages],
      count: existing.count + messages.length,
    });
    return true;
  };
}
