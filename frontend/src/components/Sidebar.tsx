"use client";

import { useEffect, useRef, useState } from "react";
import { Plus, Trash2, Eraser, Moon, Sun, Volume2, Square } from "lucide-react";
import { api } from "@/lib/api";
import { useConfig, useIdentities, useTokens, useIdentityInvalidator, useVoiceConfig, qk } from "@/lib/queries";
import { useSettings } from "@/lib/store";
import { useTts } from "@/lib/tts";
import { useQueryClient } from "@tanstack/react-query";
import type { VoicePrefs } from "@/lib/types";
import { shortModel, fmtNum, cn } from "@/lib/utils";
import { Button, Input, Textarea, SectionLabel, Spinner } from "@/components/ui/primitives";
import { Toggle, Slider, Select } from "@/components/ui/controls";
import { Tooltip } from "@/components/ui/tooltip";
import { toast } from "@/components/ui/toast";

// Human-readable label for an ElevenLabs output_format id (e.g. mp3_44100_128).
function fmtAudioFormat(f: string): string {
  const [codec, rate, kbps] = f.split("_");
  const khz = rate ? `${Number(rate) / 1000} kHz` : "";
  if (codec === "mp3") return `MP3 · ${khz}${kbps ? ` · ${kbps} kbps` : ""}`;
  if (codec === "opus") return `Opus · ${khz}${kbps ? ` · ${kbps} kbps` : ""}`;
  if (codec === "pcm") return `PCM raw · ${khz} (may not play in browser)`;
  if (codec === "ulaw") return "µ-law 8 kHz (telephony; may not play)";
  return f;
}

const PREVIEW_ID = "__voice_preview__";

function GenControl({
  label,
  hint,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  hint: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
}) {
  return (
    <div className="mb-2.5">
      <div className="flex items-center justify-between mb-1">
        <span className="font-mono text-[0.72rem] text-[var(--color-fg-muted)] flex items-center gap-1">
          {label}
          <Tooltip content={hint}>
            <span className="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full border border-[var(--color-border-strong)] text-[0.6rem] cursor-default text-[var(--color-fg-subtle)]">
              ?
            </span>
          </Tooltip>
        </span>
        <span className="font-mono text-[0.72rem] text-[var(--color-fg)]">{value.toFixed(2)}</span>
      </div>
      <Slider value={value} min={min} max={max} step={step} onValueChange={onChange} />
    </div>
  );
}

export function Sidebar({ dark, onToggleDark }: { dark: boolean; onToggleDark: () => void }) {
  const { data: config } = useConfig();
  const { data: identitiesData } = useIdentities();
  const { identityId, setIdentity, settings, update, updateGeneration, payloadReview, setPayloadReview, voice, updateVoice } =
    useSettings();
  const { data: tokens } = useTokens(identityId);
  const { data: voiceCfg, isLoading: voiceLoading, isError: voiceError, error: voiceErr } = useVoiceConfig();
  const invalidate = useIdentityInvalidator();
  const qc = useQueryClient();

  // Voice preview button state (reads the shared TTS player).
  const previewPlaying = useTts((s) => s.playingId === PREVIEW_ID);
  const previewLoading = useTts((s) => s.loadingId === PREVIEW_ID);
  const toggleTts = useTts((s) => s.toggle);

  // Once the account's voices load, pin a valid default voice/model if the
  // persisted choice is empty or no longer exists.
  useEffect(() => {
    if (!voiceCfg?.enabled) return;
    const v = useSettings.getState().voice;
    const patch: Partial<VoicePrefs> = {};
    const voiceIds = new Set(voiceCfg.voices.map((x) => x.voice_id));
    if (!v.voice_id || !voiceIds.has(v.voice_id)) {
      patch.voice_id = voiceCfg.defaults.voice_id || voiceCfg.voices[0]?.voice_id || "";
    }
    const modelIds = new Set(voiceCfg.models.map((x) => x.model_id));
    if (voiceCfg.models.length && (!v.model_id || !modelIds.has(v.model_id))) {
      patch.model_id = modelIds.has("eleven_flash_v2_5") ? "eleven_flash_v2_5" : voiceCfg.models[0].model_id;
    }
    if (Object.keys(patch).length) updateVoice(patch);
  }, [voiceCfg, updateVoice]);

  const selectedVoiceModel = voiceCfg?.models.find((m) => m.model_id === voice.model_id);

  const [newName, setNewName] = useState("");
  const [confirmClear, setConfirmClear] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);

  const identities = identitiesData?.identities ?? [];
  const models = config?.available_models ?? [];
  const currentCount = identities.find((i) => i.identity_id === identityId)?.message_count ?? 0;

  // The system-prompt textarea lives in a flex-column sidebar that scrolls, so a
  // shrinkable auto-height textarea gets crushed to a sliver. It's `shrink-0` to
  // hold its size, and we grow it to fit the content (up to a cap, then scroll).
  const sysPromptRef = useRef<HTMLTextAreaElement>(null);
  function autosizeSysPrompt() {
    const el = sysPromptRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 260) + "px";
  }
  useEffect(() => {
    if (settings.use_system_prompt) autosizeSysPrompt();
  }, [settings.use_system_prompt, settings.system_prompt_text]);

  async function createIdentity() {
    const name = newName.trim();
    if (!name) return;
    setBusy(true);
    try {
      await api.createIdentity(name);
      await qc.invalidateQueries({ queryKey: qk.identities });
      setIdentity(name);
      setNewName("");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function deleteIdentity() {
    setBusy(true);
    try {
      await api.deleteIdentity(identityId);
      await qc.invalidateQueries({ queryKey: qk.identities });
      setIdentity("default");
      setConfirmDelete(false);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function clearConversation() {
    setBusy(true);
    try {
      await api.clearConversation(identityId);
      invalidate(identityId);
      setConfirmClear(false);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="flex h-full w-[280px] shrink-0 flex-col overflow-y-auto border-r border-[var(--color-border)] bg-[var(--color-surface)] px-4 pb-8">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-[var(--color-border)] py-4">
        <div className="flex items-center gap-2">
          <span className="text-xl">🦅</span>
          <h1 className="text-[1.2rem] font-bold tracking-wide">Falcon</h1>
        </div>
        <Button size="icon" variant="ghost" onClick={onToggleDark} title="Toggle theme">
          {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </Button>
      </div>

      {/* Identity */}
      <SectionLabel>Identity</SectionLabel>
      <Select
        value={identityId}
        onValueChange={setIdentity}
        options={identities.map((i) => ({
          value: i.identity_id,
          label: `${i.identity_id} (${i.message_count} msgs)`,
        }))}
      />
      <div className="mt-2 flex gap-1.5">
        <Input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && createIdentity()}
          placeholder="New identity name…"
          className="h-8 text-[0.8rem]"
        />
        <Button size="icon" variant="secondary" onClick={createIdentity} loading={busy} title="Create">
          <Plus className="h-4 w-4" />
        </Button>
      </div>

      {identityId !== "default" && (
        <>
          {confirmDelete ? (
            <div className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-[0.75rem] text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
              Delete <b>{identityId}</b> and all its data?
              <div className="mt-2 flex gap-1.5">
                <Button size="sm" variant="danger" onClick={deleteIdentity} loading={busy}>
                  Delete
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(false)}>
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <Button
              className="mt-2 w-full justify-center"
              size="sm"
              variant="danger"
              onClick={() => setConfirmDelete(true)}
            >
              <Trash2 className="h-3.5 w-3.5" /> Delete &lsquo;{identityId}&rsquo;
            </Button>
          )}
        </>
      )}

      {currentCount > 0 && (
        <>
          {confirmClear ? (
            <div className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-[0.75rem] text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
              Permanently delete this conversation, traces, tokens, audit &amp; memory (except persona)?
              <div className="mt-2 flex gap-1.5">
                <Button size="sm" variant="danger" onClick={clearConversation} loading={busy}>
                  Confirm
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setConfirmClear(false)}>
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <Button
              className="mt-2 w-full justify-center"
              size="sm"
              variant="ghost"
              onClick={() => setConfirmClear(true)}
            >
              <Eraser className="h-3.5 w-3.5" /> Clear conversation
            </Button>
          )}
        </>
      )}

      {/* Model */}
      <SectionLabel>Model</SectionLabel>
      <Select
        value={settings.model}
        onValueChange={(v) => update({ model: v })}
        options={models.map((m) => ({ value: m, label: m }))}
      />

      {/* System Prompt */}
      <SectionLabel>System Prompt</SectionLabel>
      <Toggle
        label="System prompt"
        checked={settings.use_system_prompt}
        onCheckedChange={(v) => update({ use_system_prompt: v })}
        hint={settings.use_system_prompt ? "ON — prompt active" : "OFF — no platform prompt injected"}
      />
      {settings.use_system_prompt && (
        <Textarea
          ref={sysPromptRef}
          className="mt-2 min-h-[6rem] shrink-0 resize-none text-[0.8rem] leading-relaxed"
          rows={5}
          value={settings.system_prompt_text}
          onChange={(e) => {
            update({ system_prompt_text: e.target.value });
            autosizeSysPrompt();
          }}
          placeholder="Enter system prompt…"
        />
      )}

      {/* Persona */}
      <SectionLabel>Persona</SectionLabel>
      <Toggle
        label="Persona"
        checked={settings.use_persona}
        onCheckedChange={(v) => update({ use_persona: v })}
        hint={
          settings.use_persona
            ? "ON — persona memory injected when available"
            : "OFF — persona block excluded from context"
        }
      />

      {/* Memory Extraction */}
      <SectionLabel>Memory Extraction</SectionLabel>
      <Toggle
        label="Auto-extract memory"
        checked={settings.use_memory_extraction}
        onCheckedChange={(v) => update({ use_memory_extraction: v })}
        hint={
          settings.use_memory_extraction
            ? `ON — user facts saved after each turn via ${shortModel(config?.extraction_model ?? "openai/gpt-4o-mini")}`
            : "OFF — no automatic memory extraction"
        }
      />
      {settings.use_memory_extraction && (
        <div className="mt-1 text-[0.72rem] text-[var(--color-fg-subtle)]">
          model · {shortModel(config?.extraction_model ?? "openai/gpt-4o-mini")}
        </div>
      )}

      {/* History Truncation */}
      <SectionLabel>History Truncation</SectionLabel>
      <Input
        type="number"
        min={0}
        max={100}
        value={settings.history_max_turns}
        onChange={(e) => update({ history_max_turns: Math.max(0, Math.min(100, Number(e.target.value) || 0)) })}
      />
      {settings.history_max_turns === 0 && (
        <div className="mt-1 text-[0.72rem] text-[var(--color-fg-subtle)]">0 — no history sent to model</div>
      )}

      {/* History Mode */}
      <SectionLabel>History Mode</SectionLabel>
      <Select
        value={settings.history_mode}
        onValueChange={(v) => update({ history_mode: v as typeof settings.history_mode })}
        options={[
          { value: "raw", label: "Raw History" },
          { value: "summary", label: "Summary" },
          { value: "hybrid", label: "Hybrid (Summary + Raw)" },
        ]}
      />

      {/* Tools */}
      <SectionLabel>Tools</SectionLabel>
      <Toggle
        label="Tools (Weather · NASA APOD)"
        checked={settings.use_tools}
        onCheckedChange={(v) => update({ use_tools: v })}
        hint={
          settings.use_tools
            ? "ON — model can call get_weather & get_nasa_apod via a LangGraph agent"
            : "OFF — plain streaming inference, no tool calls"
        }
      />

      {/* Judge */}
      <SectionLabel>Judge</SectionLabel>
      <Toggle
        label="Judge"
        checked={settings.use_judge}
        onCheckedChange={(v) => update({ use_judge: v })}
        hint={settings.use_judge ? "ON — each response judged before display" : "OFF — responses shown as generated"}
      />
      {settings.use_judge && (
        <div className="mt-2">
          <Select
            value={settings.judge_model ?? models[0] ?? ""}
            onValueChange={(v) => update({ judge_model: v })}
            options={models.map((m) => ({ value: m, label: m }))}
          />
        </div>
      )}

      {/* Payload Review */}
      <SectionLabel>Payload Review</SectionLabel>
      <Toggle
        label="Payload review"
        checked={payloadReview}
        onCheckedChange={setPayloadReview}
        hint={payloadReview ? "ON — assembled payload shown before each send" : "OFF — messages sent directly"}
      />

      {/* Generation Controls */}
      <SectionLabel>Generation Controls</SectionLabel>
      <GenControl
        label="temperature"
        hint="Controls randomness. Lower = focused/deterministic; higher = creative/unpredictable."
        value={settings.generation.temperature}
        min={0}
        max={2}
        step={0.05}
        onChange={(v) => updateGeneration({ temperature: v })}
      />
      <GenControl
        label="top_p"
        hint="Nucleus sampling threshold. 1.0 = all tokens; lower cuts off unlikely words."
        value={settings.generation.top_p}
        min={0}
        max={1}
        step={0.05}
        onChange={(v) => updateGeneration({ top_p: v })}
      />
      <GenControl
        label="repetition_penalty"
        hint="Penalises tokens already used. 1.0 = no penalty; higher discourages repetition."
        value={settings.generation.repetition_penalty}
        min={1}
        max={2}
        step={0.05}
        onChange={(v) => updateGeneration({ repetition_penalty: v })}
      />
      <div className="mt-1">
        <span className="font-mono text-[0.72rem] text-[var(--color-fg-muted)]">stop_tokens</span>
        <Input
          className="mt-1 h-8 text-[0.8rem]"
          placeholder="comma-separated, e.g. <|end|>, ###"
          value={settings.generation.stop_tokens.join(", ")}
          onChange={(e) =>
            updateGeneration({
              stop_tokens: e.target.value
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
            })
          }
        />
      </div>

      {/* Dual Run */}
      <SectionLabel>Dual Run</SectionLabel>
      <Toggle
        label="Dual run logging"
        checked={settings.dual_run_enabled}
        onCheckedChange={(v) => update({ dual_run_enabled: v })}
        hint={settings.dual_run_enabled ? "ON — each message runs twice, both logged" : "OFF — single inference"}
      />
      {settings.dual_run_enabled && (
        <div className="mt-2">
          <Select
            value={settings.dual_run_state_tag}
            onValueChange={(v) => update({ dual_run_state_tag: v })}
            options={(config?.dual_run_states ?? ["Neutral"]).map((s) => ({ value: s, label: s }))}
          />
        </div>
      )}

      {/* Voice — ElevenLabs text-to-speech */}
      <SectionLabel>Voice · Text-to-Speech</SectionLabel>
      {voiceLoading ? (
        <div className="flex items-center gap-2 text-[0.75rem] text-[var(--color-fg-subtle)]">
          <Spinner /> Loading voices…
        </div>
      ) : voiceError ? (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-[0.72rem] leading-snug text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
          {(voiceErr as Error)?.message ?? "Could not load voices."}
        </div>
      ) : !voiceCfg?.enabled ? (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] p-2.5 text-[0.72rem] leading-snug text-[var(--color-fg-subtle)]">
          Add <span className="font-mono text-[var(--color-fg-muted)]">ELEVENLABS_API_KEY</span> to the backend{" "}
          <span className="font-mono text-[var(--color-fg-muted)]">.env</span> and restart to read replies aloud.
        </div>
      ) : (
        <>
          <Toggle
            label="Auto-play responses"
            checked={voice.auto_play}
            onCheckedChange={(v) => updateVoice({ auto_play: v })}
            hint={
              voice.auto_play
                ? "ON — new replies are spoken automatically"
                : "OFF — press the speaker icon on a reply to hear it"
            }
          />

          <div className="mt-2">
            <span className="font-mono text-[0.72rem] text-[var(--color-fg-muted)]">voice</span>
            <div className="mt-1 flex gap-1.5">
              <Select
                value={voice.voice_id}
                onValueChange={(v) => updateVoice({ voice_id: v })}
                placeholder="Select a voice…"
                options={voiceCfg.voices.map((v) => {
                  // ElevenLabs library names are often "Artist - descriptors";
                  // show just the artist in the trigger, the rest in the dropdown.
                  const dash = v.name.indexOf(" - ");
                  const artist = dash > 0 ? v.name.slice(0, dash).trim() : v.name;
                  const descr = dash > 0 ? v.name.slice(dash + 3).trim() : "";
                  return {
                    value: v.voice_id,
                    label: artist,
                    hint: [descr, v.category].filter(Boolean).join(" · "),
                  };
                })}
              />
              <Button
                size="icon"
                variant="secondary"
                onClick={() => toggleTts(PREVIEW_ID, "preview", true)}
                loading={previewLoading}
                title={previewPlaying ? "Stop preview" : "Preview voice"}
              >
                {previewPlaying ? <Square className="h-3.5 w-3.5 fill-current" /> : <Volume2 className="h-4 w-4" />}
              </Button>
            </div>
          </div>

          <div className="mt-2">
            <span className="font-mono text-[0.72rem] text-[var(--color-fg-muted)]">model</span>
            <Select
              className="mt-1"
              value={voice.model_id}
              onValueChange={(v) => updateVoice({ model_id: v })}
              options={voiceCfg.models.map((m) => ({ value: m.model_id, label: m.name }))}
            />
          </div>

          <div className="mt-2">
            <span className="font-mono text-[0.72rem] text-[var(--color-fg-muted)]">output format</span>
            <Select
              className="mt-1"
              value={voice.output_format}
              onValueChange={(v) => updateVoice({ output_format: v })}
              options={voiceCfg.output_formats.map((f) => ({ value: f, label: fmtAudioFormat(f) }))}
            />
          </div>

          <div className="mt-3">
            <GenControl
              label="stability"
              hint="Lower = more expressive & variable; higher = steadier and more monotone."
              value={voice.stability}
              min={0}
              max={1}
              step={0.05}
              onChange={(v) => updateVoice({ stability: v })}
            />
            <GenControl
              label="similarity"
              hint="How closely the output matches the original voice. Higher tracks it more tightly."
              value={voice.similarity_boost}
              min={0}
              max={1}
              step={0.05}
              onChange={(v) => updateVoice({ similarity_boost: v })}
            />
            {selectedVoiceModel?.can_use_style !== false && (
              <GenControl
                label="style"
                hint="Style exaggeration. 0 is fastest and most stable; higher adds emphasis but can raise latency."
                value={voice.style}
                min={0}
                max={1}
                step={0.05}
                onChange={(v) => updateVoice({ style: v })}
              />
            )}
            <GenControl
              label="speed"
              hint="Speaking rate. 1.0 is normal; below slows down, above speeds up (0.7–1.2)."
              value={voice.speed}
              min={0.7}
              max={1.2}
              step={0.05}
              onChange={(v) => updateVoice({ speed: v })}
            />
          </div>

          {selectedVoiceModel?.can_use_speaker_boost !== false && (
            <Toggle
              label="Speaker boost"
              checked={voice.use_speaker_boost}
              onCheckedChange={(v) => updateVoice({ use_speaker_boost: v })}
              hint={voice.use_speaker_boost ? "ON — boosts similarity to the original speaker" : "OFF"}
            />
          )}
        </>
      )}

      {/* Session Stats */}
      <SectionLabel>Session</SectionLabel>
      <div className="space-y-1 text-[0.8rem] text-[var(--color-fg-muted)]">
        <StatRow label="Messages" value={fmtNum(currentCount)} />
        <StatRow label="Prompt tokens" value={fmtNum(tokens?.prompt)} />
        <StatRow label="Completion tokens" value={fmtNum(tokens?.completion)} />
        <div className="border-t border-[var(--color-border)] pt-1.5">
          <StatRow label="Total" value={fmtNum(tokens?.total)} bold />
        </div>
      </div>
      <div className="mt-3 font-mono text-[0.68rem] text-[var(--color-fg-subtle)] break-all">
        mongodb://falcon/messages [{identityId}]
      </div>
      <div className="mt-2 text-[0.68rem] text-[var(--color-fg-subtle)]">model · {shortModel(settings.model)}</div>
    </aside>
  );
}

function StatRow({ label, value, bold }: { label: string; value: string; bold?: boolean }) {
  return (
    <div className="flex items-center justify-between">
      <span>{label}</span>
      <span className={cn("font-mono", bold && "font-semibold text-[var(--color-fg)]")}>{value}</span>
    </div>
  );
}
