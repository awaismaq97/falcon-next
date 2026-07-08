// Types mirroring the FastAPI backend contracts.

export type MemoryType =
  | "semantic"
  | "episodic"
  | "procedural"
  | "working"
  | "archive"
  | "persona";

export type HistoryMode = "raw" | "summary" | "hybrid";

export interface GenerationSettings {
  temperature: number;
  top_p: number;
  repetition_penalty: number;
  stop_tokens: string[];
}

export interface ChatSettings {
  model: string;
  use_system_prompt: boolean;
  system_prompt_text: string;
  use_persona: boolean;
  history_max_turns: number;
  history_mode: HistoryMode;
  use_tools: boolean;
  use_judge: boolean;
  judge_model: string | null;
  use_memory_extraction: boolean;
  dual_run_enabled: boolean;
  dual_run_state_tag: string;
  generation: GenerationSettings;
}

export interface AppConfig {
  default_model: string;
  available_models: string[];
  default_system_prompt: string;
  default_persona: PersonaFields;
  default_persona_identity: string;
  vision_model: string;
  extraction_model: string;
  summary_model: string;
  generation: GenerationSettings;
  history: { max_turns: number; modes: HistoryMode[]; default_mode: HistoryMode };
  features: {
    memory_extraction_enabled: boolean;
    audit_enabled: boolean;
    default_use_tools: boolean;
    default_use_judge: boolean;
    default_use_persona: boolean;
    default_use_system_prompt: boolean;
  };
  retrieval: { top_k_per_type: number; recency_weight: number; relevance_weight: number };
  assistant_language_patterns: string[];
  memory_types: MemoryType[];
  dual_run_states: string[];
}

export interface Identity {
  identity_id: string;
  message_count: number;
}

/** Extracted text from an uploaded document, sent with a chat turn. */
export interface DocAttachment {
  filename: string;
  text: string;
}

/** Response from POST /documents/extract. */
export interface ExtractResult {
  filename: string;
  chars: number;
  truncated: boolean;
  text: string;
}

// ── Voice / text-to-speech (ElevenLabs) ─────────────────────────────────────

/** The audio knobs sent to POST /voice/tts. */
export interface TtsParams {
  voice_id: string;
  model_id: string;
  output_format: string;
  stability: number;
  similarity_boost: number;
  style: number;
  use_speaker_boost: boolean;
  speed: number;
  language_code?: string | null;
  seed?: number | null;
}

/** Client-side voice preferences: the TTS params plus a UI-only auto-play flag. */
export interface VoicePrefs extends TtsParams {
  auto_play: boolean;
}

export interface VoiceInfo {
  voice_id: string;
  name: string;
  category?: string | null;
  labels?: Record<string, string>;
  preview_url?: string | null;
}

export interface TtsModelInfo {
  model_id: string;
  name: string;
  languages: string[];
  can_use_style: boolean;
  can_use_speaker_boost: boolean;
}

export interface VoiceConfig {
  enabled: boolean;
  voices: VoiceInfo[];
  models: TtsModelInfo[];
  output_formats: string[];
  defaults: TtsParams;
}

export interface Message {
  timestamp: string;
  role: "user" | "assistant";
  content: string;
  // client-only fields
  _streaming?: boolean;
  _images?: string[];
  _events?: ToolEvent[];
  _warning?: string;
  _judge?: JudgePayload | null;
  _suppressed?: boolean;
}

export interface PersonaFields {
  name: string;
  tone: string;
  communication_style: string;
  core_traits: string;
}

export interface MemoryEntry {
  _id: string;
  identity_id: string;
  memory_type: MemoryType;
  content: string;
  tags: string[];
  pinned: boolean;
  source: string;
  created_at: string;
  updated_at: string;
  score?: number;
  match_reason?: string;
}

export interface RetrievalResult {
  retrieved_count: number;
  total_found: number;
  reasoning: string[];
  by_type: Record<string, number>;
  entries: MemoryEntry[];
}

export interface ContextSnapshot {
  system_prompt: string | null;
  prompt_state: string;
  persona_block: { content: string; source: string } | null;
  memory_entries: { content: string; source: string }[];
  history_included: { role: string; content: string; source: string }[];
  history_dropped_turns: number;
  truncation_strategy: string;
  history_mode: string;
  history_summary: string | null;
  current_input: Record<string, unknown>;
  assembled_payload: { role: string; content: string }[];
  annotated_payload: { role: string; content: string; source: string }[];
  context_token_estimate: number;
  retrieval_timeout: boolean;
  retrieval_result: RetrievalResult | null;
  message_count: number;
}

export interface TraceStep {
  t: string;
  stage: string;
  data: unknown;
  status: string;
  elapsed_ms: number;
}

export interface Trace {
  user_timestamp: string;
  send_timestamp: string;
  user: string;
  steps: TraceStep[];
  context_snapshot: ContextSnapshot;
}

export interface AuditSummary {
  _id: string;
  identity_id: string;
  recorded_at: string;
  timestamp: string;
  model: string;
  prompt_state: string;
  context_size: number;
  context_token_estimate: number;
  usage: Record<string, number>;
  latency_ms: number;
}

export interface ToolEvent {
  type: "tool_call" | "tool_result";
  tool: string;
  args?: Record<string, unknown>;
  content?: string;
}

export interface JudgePayload {
  verdict: string;
  reason: string;
  latency_ms: number;
  model: string;
  raw: string;
  error: string | null;
}

export interface TokenTotals {
  prompt: number;
  completion: number;
  total: number;
}

// Dual-run
export interface DualRunSide {
  text: string;
  tokens: Record<string, number>;
  timestamp: string;
  latency_ms: number;
  broke_through: boolean;
  first_break: string;
}

export interface DualRunRecord {
  identity_id: string;
  model: string;
  state_tag: string;
  system_prompt: string;
  user_input: string;
  sun_instruction_active: boolean;
  run1: DualRunSide;
  run2: DualRunSide;
  any_breakthrough: boolean;
  recorded_at: string;
}

export interface DualRunStats {
  total_runs: number;
  breakthrough_count: number;
  breakthrough_rate: number;
  per_state: Record<string, { total: number; breakthroughs: number }>;
}

// Testing
export interface TestVariant {
  index: number;
  name: string;
  description: string;
  resolved_settings: Record<string, unknown>;
}

export interface TestDef {
  slug: string;
  name: string;
  description: string;
  variants: TestVariant[];
}

export interface TestRun {
  run_at: string;
  variant_idx: number;
  settings: Record<string, unknown>;
  probe_results: TestProbeResult[];
}

export interface TestProbeResult {
  probe: string;
  payload: { role: string; content: string }[];
  response: string;
  latency_ms: number;
  usage: Record<string, number> | null;
  judge: { verdict: string; reason: string } | null;
}

// SSE events
export type SSEEvent =
  | { type: "meta"; user_ts: string; model: string; logged_user_input: string; tools_enabled: boolean; judge_enabled: boolean }
  | { type: "token"; text: string }
  | { type: "tool_call"; tool: string; args: Record<string, unknown> }
  | { type: "tool_result"; tool: string; content: string }
  | { type: "message"; text: string }
  | { type: "warning"; message: string }
  | { type: "done"; response_text: string; raw_output: string; usage: Record<string, number>; latency_ms: number; suppressed: boolean; judge: JudgePayload | null; user_ts: string; asst_ts: string; tokens_total: TokenTotals; logged_user_input: string }
  | { type: "error"; message: string };
