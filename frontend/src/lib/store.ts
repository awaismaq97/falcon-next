// Client settings store (Zustand + localStorage persistence).
//
// Holds the current identity and every sidebar control, mirroring the Streamlit
// session_state. These are sent with each chat request (the backend is stateless).

import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { AppConfig, ChatSettings, HistoryMode, VoicePrefs } from "./types";

interface SettingsState {
  initialized: boolean;
  identityId: string;
  payloadReview: boolean;
  activeTab: string;
  settings: ChatSettings;
  voice: VoicePrefs;

  initFromConfig: (cfg: AppConfig) => void;
  setIdentity: (id: string) => void;
  setActiveTab: (tab: string) => void;
  setPayloadReview: (v: boolean) => void;
  update: (patch: Partial<ChatSettings>) => void;
  updateGeneration: (patch: Partial<ChatSettings["generation"]>) => void;
  updateVoice: (patch: Partial<VoicePrefs>) => void;
}

const DEFAULT_SETTINGS: ChatSettings = {
  model: "",
  use_system_prompt: false,
  system_prompt_text: "",
  use_persona: false,
  history_max_turns: 15,
  history_mode: "raw" as HistoryMode,
  use_tools: true,
  use_judge: false,
  judge_model: null,
  dual_run_enabled: false,
  dual_run_state_tag: "Neutral",
  generation: { temperature: 0, top_p: 1, repetition_penalty: 1, stop_tokens: [] },
};

// Voice defaults mirror the backend /voice/config defaults. voice_id starts
// empty and is filled from the account's first voice once config loads.
const DEFAULT_VOICE: VoicePrefs = {
  auto_play: false,
  voice_id: "",
  model_id: "eleven_flash_v2_5",
  output_format: "mp3_44100_128",
  stability: 0.5,
  similarity_boost: 0.75,
  style: 0.0,
  use_speaker_boost: true,
  speed: 1.0,
  language_code: null,
  seed: null,
};

export const useSettings = create<SettingsState>()(
  persist(
    (set, get) => ({
      initialized: false,
      identityId: "default",
      payloadReview: false,
      activeTab: "chat",
      settings: DEFAULT_SETTINGS,
      voice: DEFAULT_VOICE,

      initFromConfig: (cfg) => {
        if (get().initialized) return;
        set((s) => ({
          initialized: true,
          settings: {
            ...s.settings,
            model: s.settings.model || cfg.default_model,
            system_prompt_text: s.settings.system_prompt_text || cfg.default_system_prompt,
            history_max_turns: cfg.history.max_turns,
            use_tools: cfg.features.default_use_tools,
            use_judge: cfg.features.default_use_judge,
            use_persona: cfg.features.default_use_persona,
            use_system_prompt: cfg.features.default_use_system_prompt,
            judge_model: s.settings.judge_model || cfg.available_models[0] || cfg.default_model,
            generation: {
              temperature: cfg.generation.temperature,
              top_p: cfg.generation.top_p,
              repetition_penalty: cfg.generation.repetition_penalty,
              stop_tokens: cfg.generation.stop_tokens ?? [],
            },
          },
        }));
      },
      setIdentity: (id) => set({ identityId: id }),
      setActiveTab: (tab) => set({ activeTab: tab }),
      setPayloadReview: (v) => set({ payloadReview: v }),
      update: (patch) => set((s) => ({ settings: { ...s.settings, ...patch } })),
      updateGeneration: (patch) =>
        set((s) => ({ settings: { ...s.settings, generation: { ...s.settings.generation, ...patch } } })),
      updateVoice: (patch) => set((s) => ({ voice: { ...s.voice, ...patch } })),
    }),
    {
      name: "falcon-settings",
      // Persist identity + settings + voice + review toggle; not transient tab/init flags.
      partialize: (s) => ({
        identityId: s.identityId,
        payloadReview: s.payloadReview,
        settings: s.settings,
        voice: s.voice,
      }),
      // Older persisted state predates `voice`; backfill it so the store is
      // never missing keys after an app update.
      merge: (persisted, current) => {
        const p = (persisted ?? {}) as Partial<SettingsState>;
        return {
          ...current,
          ...p,
          voice: { ...DEFAULT_VOICE, ...(p.voice ?? {}) },
          settings: { ...DEFAULT_SETTINGS, ...(p.settings ?? {}) },
        };
      },
    },
  ),
);
