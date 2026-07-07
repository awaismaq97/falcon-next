// Typed API client for the Falcon FastAPI backend.
//
// In dev, NEXT_PUBLIC_API_BASE points at the backend (http://127.0.0.1:8017).
// In production it is empty, so requests use relative /api paths that the
// DigitalOcean App Platform ingress routes to the backend service.

import type {
  AppConfig,
  AuditSummary,
  ChatSettings,
  ContextSnapshot,
  DualRunRecord,
  DualRunStats,
  Identity,
  MemoryEntry,
  MemoryType,
  Message,
  PersonaFields,
  RetrievalResult,
  TestDef,
  TestRun,
  TokenTotals,
  Trace,
  TtsParams,
  VoiceConfig,
  ExtractResult,
} from "./types";

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

// Non-streaming requests get a hard timeout so a hung backend fails fast with a
// clear message instead of leaving the UI spinning forever. (The SSE send path
// in sse.ts deliberately has no timeout — it can stay open while the model thinks.)
const DEFAULT_TIMEOUT_MS = 30_000;

function url(path: string) {
  return `${API_BASE}${path}`;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url(path), {
      ...init,
      signal: init?.signal ?? AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "TimeoutError") {
      throw new Error("Request timed out — is the backend reachable?");
    }
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new Error(`Network error: ${(e as Error).message}`);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// Like req(), but the success body is binary audio (returned as a Blob) rather
// than JSON. Errors still come back as JSON {detail}, so decode those specially.
async function audioReq(path: string, body: unknown): Promise<Blob> {
  let res: Response;
  try {
    res = await fetch(url(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(TTS_TIMEOUT_MS),
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "TimeoutError") {
      throw new Error("Voice request timed out.");
    }
    throw new Error(`Network error: ${(e as Error).message}`);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const err = await res.json();
      detail = err.detail ?? JSON.stringify(err);
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  return res.blob();
}

// Longer ceiling for TTS: synthesising a long response can take a while, but we
// still want a hung request to eventually fail rather than spin forever.
const TTS_TIMEOUT_MS = 120_000;

export const api = {
  // ── Config ──────────────────────────────────────────────────────────────
  getConfig: () => req<AppConfig>("/api/config"),

  // ── Voice (ElevenLabs text-to-speech) ────────────────────────────────────
  getVoiceConfig: () => req<VoiceConfig>("/api/voice/config"),
  // Returns an audio Blob (mp3 by default). `preview` speaks a fixed sample so
  // the user can audition settings without a real message.
  tts: (text: string, params: TtsParams, preview = false): Promise<Blob> =>
    audioReq(preview ? "/api/voice/preview" : "/api/voice/tts", { text, ...params }),

  // ── Identities ──────────────────────────────────────────────────────────
  listIdentities: () => req<{ identities: Identity[]; default: string }>("/api/identities"),
  createIdentity: (identity_id: string) =>
    req<Identity>("/api/identities", { method: "POST", body: JSON.stringify({ identity_id }) }),
  deleteIdentity: (id: string) =>
    req<{ deleted: string }>(`/api/identities/${encodeURIComponent(id)}`, { method: "DELETE" }),
  loadHistory: (id: string, limit = 2000) =>
    req<{ identity_id: string; messages: Message[]; count: number }>(
      `/api/identities/${encodeURIComponent(id)}/history?limit=${limit}`,
    ),
  clearConversation: (id: string) =>
    req<{ cleared: string }>(`/api/identities/${encodeURIComponent(id)}/clear`, { method: "POST" }),
  getTokens: (id: string) => req<TokenTotals>(`/api/identities/${encodeURIComponent(id)}/tokens`),
  saveMessages: (id: string, entries: Message[]) =>
    req<{ identity_id: string; count: number }>(
      `/api/identities/${encodeURIComponent(id)}/messages`,
      { method: "PUT", body: JSON.stringify({ entries }) },
    ),

  // ── Documents ─────────────────────────────────────────────────────────────
  // Multipart upload → extracted text. No JSON Content-Type (the browser sets
  // the multipart boundary itself).
  extractDocument: async (file: File): Promise<ExtractResult> => {
    const fd = new FormData();
    fd.append("file", file);
    let res: Response;
    try {
      res = await fetch(url("/api/documents/extract"), {
        method: "POST",
        body: fd,
        signal: AbortSignal.timeout(60_000),
      });
    } catch (e) {
      if (e instanceof DOMException && e.name === "TimeoutError") {
        throw new Error("Document upload timed out.");
      }
      throw new Error(`Network error: ${(e as Error).message}`);
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json()).detail ?? detail;
      } catch {
        /* keep statusText */
      }
      throw new Error(detail);
    }
    return res.json() as Promise<ExtractResult>;
  },

  // ── Chat ────────────────────────────────────────────────────────────────
  preview: (identity_id: string, message: string, settings: ChatSettings) =>
    req<{
      raw_payload: { role: string; content: string }[];
      annotated_payload: { role: string; content: string; source: string }[];
      context_snapshot: ContextSnapshot;
      retrieved_entries: MemoryEntry[];
      system_prompt: string;
      history_summary: string | null;
    }>("/api/chat/preview", {
      method: "POST",
      body: JSON.stringify({ identity_id, message, settings }),
    }),

  // ── Memory ──────────────────────────────────────────────────────────────
  listMemory: (id: string, type?: MemoryType) =>
    req<{ entries: MemoryEntry[]; count: number }>(
      `/api/identities/${encodeURIComponent(id)}/memory${type ? `?memory_type=${type}` : ""}`,
    ),
  addMemory: (
    id: string,
    body: { memory_type: MemoryType; content: string; tags: string[]; pinned: boolean; source?: string },
  ) =>
    req<{ _id: string }>(`/api/identities/${encodeURIComponent(id)}/memory`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateMemory: (memId: string, body: { content?: string; tags?: string[]; pinned?: boolean }) =>
    req<{ updated: string }>(`/api/memory/${memId}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteMemory: (memId: string) => req<{ deleted: string }>(`/api/memory/${memId}`, { method: "DELETE" }),
  clearMemoryType: (id: string, type: MemoryType) =>
    req<{ deleted_count: number }>(
      `/api/identities/${encodeURIComponent(id)}/memory?memory_type=${type}`,
      { method: "DELETE" },
    ),
  getPersona: (id: string) =>
    req<{ exists: boolean; _id: string | null; fields: PersonaFields; raw: string }>(
      `/api/identities/${encodeURIComponent(id)}/persona`,
    ),
  savePersona: (id: string, fields: PersonaFields) =>
    req<{ _id: string; raw: string }>(`/api/identities/${encodeURIComponent(id)}/persona`, {
      method: "PUT",
      body: JSON.stringify(fields),
    }),
  testRetrieval: (id: string, query: string, use_persona: boolean) =>
    req<RetrievalResult>(`/api/identities/${encodeURIComponent(id)}/memory/retrieve`, {
      method: "POST",
      body: JSON.stringify({ query, use_persona }),
    }),
  exportMemory: (id: string) => req<unknown>(`/api/identities/${encodeURIComponent(id)}/memory/export`),

  // ── Traces / Context ─────────────────────────────────────────────────────
  traceIndex: (id: string) =>
    req<{ timestamps: string[] }>(`/api/identities/${encodeURIComponent(id)}/trace-index`),
  listTraces: (id: string, skip = 0, limit = 25) =>
    req<{ traces: Trace[]; total: number; skip: number; limit: number }>(
      `/api/identities/${encodeURIComponent(id)}/traces?skip=${skip}&limit=${limit}`,
    ),
  latestContext: (id: string) =>
    req<{ exists: boolean; user_timestamp?: string; user?: string; context_snapshot: ContextSnapshot | null }>(
      `/api/identities/${encodeURIComponent(id)}/context/latest`,
    ),
  getTrace: (id: string, ts: string) =>
    req<Trace>(`/api/identities/${encodeURIComponent(id)}/traces/${encodeURIComponent(ts)}`),
  getTracePayload: (id: string, ts: string) =>
    req<{ payload: { role: string; content: string }[] | null }>(
      `/api/identities/${encodeURIComponent(id)}/traces/${encodeURIComponent(ts)}/payload`,
    ),
  deleteTrace: (id: string, ts: string) =>
    req<{ deleted: string }>(
      `/api/identities/${encodeURIComponent(id)}/traces/${encodeURIComponent(ts)}`,
      { method: "DELETE" },
    ),

  // ── Audit ────────────────────────────────────────────────────────────────
  auditSummaries: (id: string, skip = 0, limit = 25) =>
    req<{ records: AuditSummary[]; total: number; skip: number; limit: number }>(
      `/api/identities/${encodeURIComponent(id)}/audit/summaries?skip=${skip}&limit=${limit}`,
    ),
  allAuditSummaries: (limit = 200) => req<{ records: AuditSummary[] }>(`/api/audit/summaries?limit=${limit}`),
  auditDetail: (recordId: string) => req<Record<string, unknown>>(`/api/audit/${recordId}`),
  exportAudit: (id: string) => req<unknown>(`/api/identities/${encodeURIComponent(id)}/audit/export`),
  clearAudit: (id: string) =>
    req<{ deleted_count: number }>(`/api/identities/${encodeURIComponent(id)}/audit`, { method: "DELETE" }),

  // ── Dual run ───────────────────────────────────────────────────────────────
  dualRuns: (id: string) =>
    req<{ records: DualRunRecord[]; stats: DualRunStats }>(
      `/api/identities/${encodeURIComponent(id)}/dual-run`,
    ),
  exportDualRuns: (id: string) => req<unknown>(`/api/identities/${encodeURIComponent(id)}/dual-run/export`),
  deleteDualRuns: (id: string) =>
    req<{ deleted_count: number }>(`/api/identities/${encodeURIComponent(id)}/dual-run`, { method: "DELETE" }),

  // ── Testing ────────────────────────────────────────────────────────────────
  testingRegistry: () => req<{ tests: TestDef[] }>("/api/testing/registry"),
  testingHistory: (slug: string) => req<{ slug: string; runs: TestRun[] }>(`/api/testing/${slug}/history`),
  testingReport: (slug: string) => req<{ slug: string; report: string }>(`/api/testing/${slug}/report`),
  testingRun: (slug: string, variant: number) =>
    req<{ slug: string; record: TestRun }>(`/api/testing/${slug}/run`, {
      method: "POST",
      body: JSON.stringify({ test_id: slug, variant: String(variant) }),
    }),
};
