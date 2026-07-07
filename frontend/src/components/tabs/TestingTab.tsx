"use client";

import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Play, Download, ChevronRight } from "lucide-react";
import { api } from "@/lib/api";
import { useTestingRegistry, useTestingHistory, useTestingReport } from "@/lib/queries";
import type { TestRun } from "@/lib/types";
import { Button, Badge, Spinner, Card } from "@/components/ui/primitives";
import { Select } from "@/components/ui/controls";
import { JsonView } from "@/components/JsonView";
import { Markdown } from "@/components/Markdown";
import { cn, downloadText, fmtTime } from "@/lib/utils";
import { toast } from "@/components/ui/toast";

function RunView({ run, idx }: { run: TestRun; idx: number }) {
  const [open, setOpen] = useState(idx === 0);
  const s = run.settings as Record<string, unknown>;
  const probes = run.probe_results ?? [];

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)]">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center gap-2 px-3 py-2 text-left">
        <ChevronRight className={cn("h-4 w-4 text-[var(--color-fg-subtle)] transition-transform", open && "rotate-90")} />
        <span className="text-[0.82rem] font-medium">Run {idx + 1}</span>
        <span className="font-mono text-[0.72rem] text-[var(--color-fg-subtle)]">{fmtTime(run.run_at)}</span>
        <Badge color="gray">{String(s.variant_name ?? `Variant ${run.variant_idx}`)}</Badge>
        <span className="ml-auto text-[0.72rem] text-[var(--color-fg-subtle)]">{probes.length} probes</span>
      </button>
      {open && (
        <div className="space-y-3 border-t border-[var(--color-border)] p-3">
          <div className="flex flex-wrap gap-1.5">
            <Badge color="gray">{String(s.model ?? "?").split("/").pop()}</Badge>
            <Badge color={s.system_prompt_on ? "blue" : "gray"}>SP: {s.system_prompt_on ? "ON" : "OFF"}</Badge>
            <Badge color="gray">memory: {s.use_memory ? "ON" : "OFF"}</Badge>
            <Badge color="gray">judge: {s.use_judge ? "ON" : "OFF"}</Badge>
            <Badge color="gray">noise: {String(s.noise_level ?? 0)}</Badge>
          </div>
          {probes.map((pr, i) => (
            <div key={i} className="rounded-lg border border-[var(--color-border)] p-2.5">
              <div className="mb-1.5 text-[0.8rem] font-medium">
                Probe {i + 1}: <span className="italic text-[var(--color-fg-muted)]">{pr.probe.slice(0, 120)}</span>
              </div>
              <div className="grid gap-2 md:grid-cols-2">
                <div>
                  <div className="mb-1 font-mono text-[0.68rem] uppercase text-[var(--color-amber)]">payload ({pr.payload.length})</div>
                  <JsonView data={pr.payload} maxHeight="200px" collapsed />
                </div>
                <div>
                  <div className="mb-1 flex items-center gap-2 font-mono text-[0.68rem] uppercase text-[var(--color-green)]">
                    output
                    <span className="text-[var(--color-fg-subtle)]">
                      {pr.usage?.total_tokens ?? "?"} tok · {pr.latency_ms}ms
                      {pr.judge && ` · judge: ${pr.judge.verdict}`}
                    </span>
                  </div>
                  <div className="max-h-[200px] overflow-y-auto whitespace-pre-wrap rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] p-2 text-[0.8rem]">
                    {pr.response.slice(0, 800)}
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function TestingTab() {
  const { data: registry, isLoading } = useTestingRegistry();
  const qc = useQueryClient();
  const [slug, setSlug] = useState("");
  const [variant, setVariant] = useState(0);
  const [running, setRunning] = useState(false);

  const tests = registry?.tests ?? [];
  useEffect(() => {
    if (!slug && tests.length) setSlug(tests[0].slug);
  }, [tests, slug]);

  const test = tests.find((t) => t.slug === slug);
  const { data: history } = useTestingHistory(slug);
  const { data: report } = useTestingReport(slug);

  async function run() {
    setRunning(true);
    try {
      await api.testingRun(slug, variant);
      qc.invalidateQueries({ queryKey: ["testing-history", slug] });
      qc.invalidateQueries({ queryKey: ["testing-report", slug] });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setRunning(false);
    }
  }

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 p-8 text-[var(--color-fg-subtle)]">
        <Spinner /> Loading test registry…
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-5">
      <div>
        <h2 className="text-[1rem] font-semibold">Continuity testing</h2>
        <p className="text-[0.78rem] text-[var(--color-fg-subtle)]">
          Check whether identity and behavior persist when conditions change (model swap, context noise, prompt on/off).
        </p>
      </div>

      <Card>
        <div className="grid gap-2 sm:grid-cols-2">
          <Select
            value={slug}
            onValueChange={(v) => {
              setSlug(v);
              setVariant(0);
            }}
            options={tests.map((t) => ({ value: t.slug, label: t.name }))}
          />
          {test && (
            <Select
              value={String(variant)}
              onValueChange={(v) => setVariant(Number(v))}
              options={test.variants.map((v) => ({ value: String(v.index), label: v.name }))}
            />
          )}
        </div>
        {test && <p className="mt-2 text-[0.78rem] text-[var(--color-fg-muted)]">{test.description}</p>}
        {test?.variants[variant] && (
          <>
            <p className="mt-1 text-[0.76rem] italic text-[var(--color-fg-subtle)]">
              {test.variants[variant].description}
            </p>
            <div className="mt-2">
              <JsonView data={test.variants[variant].resolved_settings} maxHeight="160px" collapsed />
            </div>
          </>
        )}
        <div className="mt-3">
          <Button variant="primary" size="sm" onClick={run} loading={running}>
            <Play className="h-3.5 w-3.5" /> {running ? "Running (live API, 10–30s)…" : "Run variant"}
          </Button>
        </div>
      </Card>

      <div className="flex items-center justify-between">
        <h3 className="text-[0.9rem] font-semibold">Run history</h3>
        {report?.report && (
          <Button size="sm" variant="secondary" onClick={() => downloadText(`falcon_${slug}_report.md`, report.report)}>
            <Download className="h-3.5 w-3.5" /> Report (.md)
          </Button>
        )}
      </div>

      {(history?.runs?.length ?? 0) === 0 ? (
        <p className="text-[0.85rem] text-[var(--color-fg-subtle)]">No runs yet. Select a variant and click Run.</p>
      ) : (
        <div className="space-y-1.5">
          {history!.runs.map((r, i) => (
            <RunView key={i} run={r} idx={i} />
          ))}
        </div>
      )}

      {report?.report && (
        <details className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-3">
          <summary className="cursor-pointer text-[0.85rem] font-semibold">Full report preview</summary>
          <div className="mt-2">
            <Markdown>{report.report}</Markdown>
          </div>
        </details>
      )}
    </div>
  );
}
