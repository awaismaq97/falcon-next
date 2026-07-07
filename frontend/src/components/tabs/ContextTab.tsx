"use client";

import { Download } from "lucide-react";
import { useLatestContext } from "@/lib/queries";
import { useSettings } from "@/lib/store";
import { JsonView } from "@/components/JsonView";
import { Button, Badge, Spinner, Card } from "@/components/ui/primitives";
import { downloadJSON } from "@/lib/utils";

export function ContextTab() {
  const identityId = useSettings((s) => s.identityId);
  const { data, isLoading } = useLatestContext(identityId);

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 p-8 text-[var(--color-fg-subtle)]">
        <Spinner /> Loading context…
      </div>
    );
  }

  const snap = data?.context_snapshot;
  if (!data?.exists || !snap) {
    return (
      <div className="p-8 text-[0.85rem] text-[var(--color-fg-subtle)]">
        No context yet. Send a message to see the full assembled context for that turn.
      </div>
    );
  }

  const mem = snap.retrieval_result;

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-5">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <h2 className="text-[1rem] font-semibold">Context snapshot — last turn</h2>
          <p className="break-words text-[0.78rem] text-[var(--color-fg-subtle)]">
            For: <span className="font-mono">{data.user?.slice(0, 80)}</span>
          </p>
        </div>
        <Button
          size="sm"
          variant="secondary"
          className="shrink-0"
          onClick={() => downloadJSON(`falcon_context_${identityId}.json`, snap)}
        >
          <Download className="h-3.5 w-3.5" /> Export
        </Button>
      </div>

      <div className="flex flex-wrap gap-2">
        <Badge color={snap.prompt_state === "present" ? "blue" : "gray"}>system prompt: {snap.prompt_state}</Badge>
        <Badge color={snap.persona_block ? "green" : "gray"}>persona: {snap.persona_block ? "injected" : "off"}</Badge>
        <Badge color="gray">history mode: {snap.history_mode}</Badge>
        <Badge color="gray">history included: {snap.history_included?.length ?? 0}</Badge>
        {snap.history_dropped_turns > 0 && <Badge color="amber">dropped: {snap.history_dropped_turns}</Badge>}
        <Badge color="gray">~{snap.context_token_estimate} tokens</Badge>
        <Badge color="gray">{snap.message_count} messages</Badge>
        {snap.retrieval_timeout && <Badge color="red">retrieval timeout</Badge>}
      </div>

      {snap.persona_block && (
        <Card>
          <div className="mb-1 text-[0.78rem] font-semibold text-[var(--color-fg-muted)]">Persona block</div>
          <pre className="whitespace-pre-wrap text-[0.8rem] text-[var(--color-fg)]">{snap.persona_block.content}</pre>
        </Card>
      )}

      {snap.system_prompt && (
        <Card>
          <div className="mb-1 text-[0.78rem] font-semibold text-[var(--color-fg-muted)]">System prompt</div>
          <pre className="whitespace-pre-wrap text-[0.8rem] text-[var(--color-fg)]">{snap.system_prompt}</pre>
        </Card>
      )}

      <div>
        <div className="mb-1.5 text-[0.85rem] font-semibold">
          Retrieved memory {mem ? `(${mem.retrieved_count} of ${mem.total_found} found)` : "(none)"}
        </div>
        {mem && mem.entries.length > 0 ? (
          <div className="space-y-1.5">
            {mem.entries.map((e, i) => (
              <div key={i} className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-2.5">
                <div className="flex items-center gap-1.5">
                  <Badge color="blue">{e.memory_type}</Badge>
                  {e.match_reason && <span className="text-[0.72rem] text-[var(--color-fg-subtle)]">{e.match_reason}</span>}
                  {typeof e.score === "number" && (
                    <span className="text-[0.72rem] text-[var(--color-fg-subtle)]">score {e.score}</span>
                  )}
                </div>
                <div className="mt-1 text-[0.82rem] text-[var(--color-fg)]">{e.content}</div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-[0.8rem] text-[var(--color-fg-subtle)]">No memory retrieved for this turn.</p>
        )}
      </div>

      {snap.history_summary && (
        <Card>
          <div className="mb-1 text-[0.78rem] font-semibold text-[var(--color-fg-muted)]">History summary</div>
          <pre className="whitespace-pre-wrap text-[0.8rem] text-[var(--color-fg)]">{snap.history_summary}</pre>
        </Card>
      )}

      <div>
        <div className="mb-1.5 text-[0.85rem] font-semibold">Assembled payload (exact messages sent)</div>
        <JsonView data={snap.assembled_payload} maxHeight="440px" />
      </div>

      <div>
        <div className="mb-1.5 text-[0.85rem] font-semibold">Annotated payload (with source labels)</div>
        <JsonView data={snap.annotated_payload} maxHeight="360px" collapsed />
      </div>
    </div>
  );
}
