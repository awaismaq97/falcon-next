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

export interface SystemPromptData {
  exists: boolean;
  system_prompt: string;
  use_system_prompt: boolean;
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
  /** Where /documents/extract already stored it, so the model can cite the id. */
  storage_id?: string;
  /** Whether the original file was kept and can be offered as a download. */
  has_file?: boolean;
}

/** Response from POST /documents/extract. */
export interface ExtractResult {
  filename: string;
  chars: number;
  truncated: boolean;
  text: string;
  /** Whether the text was durably stored (needs identity_id on the upload). */
  saved: boolean;
  storage_id: string;
  save_error: string;
  /** True when the ORIGINAL file was kept, not just its extracted text. */
  has_file: boolean;
  /** Path to the original, downloadable byte-for-byte. Empty if none was kept. */
  download_url: string;
  bytes: number;
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
  /** Set by the server on results the watcher injected. Not client-only — it is
   *  persisted, round-tripped by the Logs editor, and styles the agent-result
   *  block. */
  _watcher?: boolean;
}

export interface PersonaFields {
  name: string;
  tone: string;
  communication_style: string;
  core_traits: string;
}

export interface PersonaSummary {
  _id: string;
  fields: PersonaFields;
  raw: string;
  pinned: boolean;
  /** True if this persona will actually be composed into the payload. */
  active: boolean;
  created_at: string;
}

export interface PersonasResponse {
  identity_id: string;
  personas: PersonaSummary[];
  count: number;
  default_fields: PersonaFields;
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
  persona_blocks?: { content: string; source: string }[];
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

// ---------------------------------------------------------------------------
// Categories
// ---------------------------------------------------------------------------

export interface Category {
  _id: string;
  identity_id: string;
  name: string;
  created_at: string;
  message_count: number;
}

export interface CategoryMessage {
  _id: string;
  identity_id: string;
  category_id: string;
  category_name: string;
  user_message: string;
  assistant_response: string;
  user_ts: string;
  asst_ts: string;
  recorded_at: string;
  /** Present when the LLM hallucinated a category name and the message was stored in "Other" as a fallback. */
  hallucinated_category?: string;
}

export interface CategoryMessagesPage {
  messages: CategoryMessage[];
  total: number;
  skip: number;
  limit: number;
}

// ---------------------------------------------------------------------------
// Watcher
// ---------------------------------------------------------------------------

export interface WatcherLogEntry {
  identity_id: string;
  msg_id: string;
  command: string;
  payload: string;
  result: string;
  latency_ms: number;
  error: boolean;
  recorded_at: string;
}

export interface WatcherStatus {
  identity_id: string;
  running: boolean;
  enabled: boolean;
}

export interface WatcherAgent {
  name: string;
  /** "generated" tools were spawned at runtime and carry their source. */
  kind: "builtin" | "generated";
  deletable: boolean;
  /** Spawn prompt for generated tools; first docstring line for built-ins. */
  summary: string;
  code: string | null;
  revision: number | null;
  created_at: string | null;
  /** When to reach for this tool — the same text the model is given. */
  use_when?: string;
  /** What the payload should contain. Empty for tools that take none. */
  payload_hint?: string;
  /** A working payload, used to prefill the run box. */
  example?: string;
  /** True when running it destroys something. The UI asks twice. */
  destructive?: boolean;
}

/** One direct run of a tool from the Watcher Agents tab. */
export interface WatcherAgentRun {
  agent: string;
  identity_id: string;
  run_id: string;
  payload: string;
  result: string;
  latency_ms: number;
  error: boolean;
}

/** The watcher persona: two authored halves around a derived command list. */
export interface WatcherPersona {
  /** Editable. Explains the [AGENT: …] command format. */
  preamble: string;
  /** Editable. The RULES block appended after the commands. */
  rules: string;
  /** Read-only — rebuilt from the live tool registry on every read. */
  commands: string;
  /** The full text the model receives. */
  assembled: string;
  updated_at: string | null;
  updated_by: string;
  is_default: boolean;
}

/** A tweet proposed by the agent, awaiting the user's Post / Reject decision. */
export interface StagedTweet {
  code: string;
  identity_id: string;
  text: string;
  status: "pending" | "posted" | "cancelled" | "expired" | "failed";
  staged_at: string;
  resolved_at: string | null;
  /** Tweet URL once posted; on a failed attempt, why X refused. */
  result: string;
  /** Failed attempts so far. A rejected post leaves the tweet pending and retryable. */
  attempts?: number;
  /** True once the user has rewritten the agent's draft. */
  edited?: boolean;
  /** Server-side character limit, so the editor's counter matches what posts. */
  max_chars?: number;
}

/** A research job as returned by the list endpoint — no findings or report. */
export interface ResearchJobSummary {
  job_id: string;
  identity_id: string;
  question: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  rounds_done: number;
  max_rounds: number;
  queries_run: string[];
  sources: string[];
  error: string;
  provider: string;
}

export interface ResearchFinding {
  round: number;
  url: string;
  note: string;
}

/** The full job, from the detail endpoint. */
export interface ResearchJob extends ResearchJobSummary {
  findings: ResearchFinding[];
  report: string;
}

/** Body for creating an agent. `name` may be blank — the backend derives one. */
export interface WatcherAgentCreate {
  purpose: string;
  name?: string;
}

export interface WatcherAgentCreated {
  agent: WatcherAgent;
  message: string;
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
