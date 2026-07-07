"use client";

import { useEffect, useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { useQueryClient } from "@tanstack/react-query";
import { Download, Pin, PinOff, Trash2, Save, Search, Plus } from "lucide-react";
import { api } from "@/lib/api";
import { useConfig, useMemory, usePersona } from "@/lib/queries";
import { useSettings } from "@/lib/store";
import type { MemoryEntry, MemoryType, PersonaFields, RetrievalResult } from "@/lib/types";
import { Button, Input, Textarea, Badge, Spinner, Card } from "@/components/ui/primitives";
import { JsonView } from "@/components/JsonView";
import { cn, downloadJSON } from "@/lib/utils";
import { toast } from "@/components/ui/toast";

function PersonaEditor({ identityId }: { identityId: string }) {
  const { data, isLoading } = usePersona(identityId);
  const qc = useQueryClient();
  const [fields, setFields] = useState<PersonaFields>({ name: "", tone: "", communication_style: "", core_traits: "" });
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (data) setFields(data.fields);
  }, [data]);

  async function save() {
    setSaving(true);
    try {
      await api.savePersona(identityId, fields);
      qc.invalidateQueries({ queryKey: ["persona", identityId] });
      qc.invalidateQueries({ queryKey: ["memory", identityId] });
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  if (isLoading) return <Spinner />;

  return (
    <Card>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-[0.9rem] font-semibold">Persona {data?.exists ? "" : "(not saved yet — from config defaults)"}</h3>
      </div>
      <div className="grid gap-2.5 sm:grid-cols-2">
        <label className="block">
          <span className="mb-1 block text-[0.72rem] text-[var(--color-fg-muted)]">Name</span>
          <Input value={fields.name} onChange={(e) => setFields({ ...fields, name: e.target.value })} />
        </label>
        <label className="block">
          <span className="mb-1 block text-[0.72rem] text-[var(--color-fg-muted)]">Tone</span>
          <Input value={fields.tone} onChange={(e) => setFields({ ...fields, tone: e.target.value })} />
        </label>
      </div>
      <label className="mt-2.5 block">
        <span className="mb-1 block text-[0.72rem] text-[var(--color-fg-muted)]">Communication style</span>
        <Input
          value={fields.communication_style}
          onChange={(e) => setFields({ ...fields, communication_style: e.target.value })}
        />
      </label>
      <label className="mt-2.5 block">
        <span className="mb-1 block text-[0.72rem] text-[var(--color-fg-muted)]">Core traits</span>
        <Textarea
          rows={5}
          value={fields.core_traits}
          onChange={(e) => setFields({ ...fields, core_traits: e.target.value })}
        />
      </label>
      <div className="mt-3">
        <Button variant="primary" size="sm" onClick={save} loading={saving}>
          <Save className="h-3.5 w-3.5" /> {saved ? "Saved!" : "Save persona"}
        </Button>
      </div>
    </Card>
  );
}

function RetrievalTester({ identityId }: { identityId: string }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<RetrievalResult | null>(null);
  const [loading, setLoading] = useState(false);

  async function run() {
    if (!query.trim()) return;
    setLoading(true);
    try {
      setResult(await api.testRetrieval(identityId, query, true));
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <Card>
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center gap-2 text-[0.85rem] font-semibold">
        <Search className="h-4 w-4" /> Test retrieval {open ? "▾" : "▸"}
      </button>
      {open && (
        <div className="mt-3 space-y-2">
          <div className="flex gap-2">
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && run()}
              placeholder="Type a query to test retrieval…"
            />
            <Button variant="primary" size="sm" onClick={run} loading={loading}>
              Run
            </Button>
          </div>
          {result && (
            <div>
              <div className="mb-1 text-[0.78rem] text-[var(--color-fg-muted)]">
                Found {result.retrieved_count} entries
              </div>
              <JsonView data={result} maxHeight="300px" />
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function EntryCard({ entry, onChanged }: { entry: MemoryEntry; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState(entry.content);
  const [tags, setTags] = useState(entry.tags.join(", "));

  async function togglePin() {
    await api.updateMemory(entry._id, { pinned: !entry.pinned });
    onChanged();
  }
  async function save() {
    await api.updateMemory(entry._id, {
      content,
      tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
    });
    setEditing(false);
    onChanged();
  }
  async function del() {
    await api.deleteMemory(entry._id);
    onChanged();
  }

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-2.5">
      {editing ? (
        <div className="space-y-2">
          <Textarea rows={3} value={content} onChange={(e) => setContent(e.target.value)} />
          <Input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="tags, comma-separated" />
          <div className="flex gap-2">
            <Button size="sm" variant="primary" onClick={save}>
              Save
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <>
          <div className="text-[0.83rem] text-[var(--color-fg)]">{entry.content}</div>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            {entry.pinned && <Badge color="amber">pinned</Badge>}
            <Badge color="gray">{entry.source}</Badge>
            {entry.tags.map((t) => (
              <Badge key={t} color="blue">
                #{t}
              </Badge>
            ))}
            <span className="ml-auto flex items-center gap-0.5">
              <button onClick={togglePin} className="rounded p-1 text-[var(--color-fg-subtle)] hover:text-[var(--color-fg)]" title={entry.pinned ? "Unpin" : "Pin"}>
                {entry.pinned ? <PinOff className="h-3.5 w-3.5" /> : <Pin className="h-3.5 w-3.5" />}
              </button>
              <button onClick={() => setEditing(true)} className="rounded px-1.5 py-1 text-[0.72rem] text-[var(--color-fg-subtle)] hover:text-[var(--color-fg)]">
                edit
              </button>
              <button onClick={del} className="rounded p-1 text-[var(--color-fg-subtle)] hover:text-[var(--color-red)]" title="Delete">
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </span>
          </div>
        </>
      )}
    </div>
  );
}

function TypePanel({ identityId, type }: { identityId: string; type: MemoryType }) {
  const { data, isLoading, refetch } = useMemory(identityId, type);
  const qc = useQueryClient();
  const [content, setContent] = useState("");
  const [tags, setTags] = useState("");
  const [pinned, setPinned] = useState(false);
  const [adding, setAdding] = useState(false);

  const entries = data?.entries ?? [];
  const refresh = () => {
    refetch();
    qc.invalidateQueries({ queryKey: ["memory", identityId] });
  };

  async function add() {
    if (!content.trim()) return;
    setAdding(true);
    try {
      await api.addMemory(identityId, {
        memory_type: type,
        content,
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
        pinned,
      });
      setContent("");
      setTags("");
      setPinned(false);
      refresh();
    } finally {
      setAdding(false);
    }
  }

  async function clearAll() {
    if (!confirm(`Delete all ${type} entries?`)) return;
    await api.clearMemoryType(identityId, type);
    refresh();
  }

  return (
    <div className="space-y-3">
      <Card>
        <div className="mb-2 text-[0.8rem] font-semibold">Add {type} entry</div>
        <Textarea rows={2} value={content} onChange={(e) => setContent(e.target.value)} placeholder="Content…" />
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Input className="max-w-xs" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="tags, comma-separated" />
          <label className="flex items-center gap-1.5 text-[0.78rem] text-[var(--color-fg-muted)]">
            <input type="checkbox" checked={pinned} onChange={(e) => setPinned(e.target.checked)} /> pinned
          </label>
          <Button size="sm" variant="primary" onClick={add} loading={adding}>
            <Plus className="h-3.5 w-3.5" /> Add
          </Button>
        </div>
      </Card>

      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 truncate text-[0.78rem] text-[var(--color-fg-subtle)]">
          {isLoading ? "Loading…" : `${entries.length} ${type} entr${entries.length !== 1 ? "ies" : "y"}`}
        </span>
        {entries.length > 0 && (
          <Button size="sm" variant="danger" className="shrink-0" onClick={clearAll}>
            Clear all
          </Button>
        )}
      </div>

      <div className="space-y-1.5">
        {entries.map((e) => (
          <EntryCard key={e._id} entry={e} onChanged={refresh} />
        ))}
      </div>
    </div>
  );
}

export function MemoryTab() {
  const identityId = useSettings((s) => s.identityId);
  const { data: config } = useConfig();
  const [activeType, setActiveType] = useState<MemoryType>("episodic");
  const types = (config?.memory_types ?? ["semantic", "episodic", "procedural", "working", "archive"]) as MemoryType[];

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-5">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <h2 className="text-[1rem] font-semibold">Memory</h2>
          <p className="break-words text-[0.78rem] text-[var(--color-fg-subtle)]">
            User-controlled memory for <span className="font-mono">{identityId}</span>. All retrieval is visible.
          </p>
        </div>
        <Button
          size="sm"
          variant="secondary"
          className="shrink-0"
          onClick={async () => downloadJSON(`falcon_memory_${identityId}.json`, await api.exportMemory(identityId))}
        >
          <Download className="h-3.5 w-3.5" /> Export
        </Button>
      </div>

      {config && !config.features.memory_extraction_enabled && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[0.8rem] text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
          🔕 Automatic memory extraction is disabled (config).
        </div>
      )}

      <RetrievalTester identityId={identityId} />
      <PersonaEditor identityId={identityId} />

      <Tabs.Root value={activeType} onValueChange={(v) => setActiveType(v as MemoryType)}>
        <Tabs.List className="flex gap-1 border-b border-[var(--color-border)]">
          {types.map((t) => (
            <Tabs.Trigger
              key={t}
              value={t}
              className={cn(
                "border-b-2 border-transparent px-3 py-2 text-[0.8rem] capitalize text-[var(--color-fg-muted)]",
                "data-[state=active]:border-[var(--color-fg)] data-[state=active]:text-[var(--color-fg)]",
              )}
            >
              {t}
            </Tabs.Trigger>
          ))}
        </Tabs.List>
        {types.map((t) => (
          <Tabs.Content key={t} value={t} className="pt-3">
            {activeType === t && <TypePanel identityId={identityId} type={t} />}
          </Tabs.Content>
        ))}
      </Tabs.Root>
    </div>
  );
}
