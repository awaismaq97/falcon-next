"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { JsonView } from "@/components/JsonView";
import { Spinner, Badge } from "@/components/ui/primitives";

export function ContextDialog({
  identityId,
  userTs,
  open,
  onOpenChange,
}: {
  identityId: string;
  userTs: string;
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["trace-full", identityId, userTs],
    queryFn: () => api.getTrace(identityId, userTs),
    enabled: open && !!userTs,
  });

  const snap = data?.context_snapshot;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title="Context sent for this turn">
        {isLoading || !snap ? (
          <div className="flex items-center gap-2 py-8 text-[var(--color-fg-subtle)]">
            <Spinner /> Loading context…
          </div>
        ) : (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2 text-[0.75rem]">
              <Badge color={snap.prompt_state === "present" ? "blue" : "gray"}>
                system prompt: {snap.prompt_state}
              </Badge>
              <Badge color={snap.persona_block ? "green" : "gray"}>
                persona: {snap.persona_block ? "on" : "off"}
              </Badge>
              <Badge color="gray">history mode: {snap.history_mode}</Badge>
              <Badge color="gray">~{snap.context_token_estimate} tokens</Badge>
              <Badge color="gray">{snap.message_count} messages</Badge>
              {snap.history_dropped_turns > 0 && (
                <Badge color="amber">{snap.history_dropped_turns} history dropped</Badge>
              )}
            </div>

            {snap.retrieval_result && snap.retrieval_result.entries.length > 0 && (
              <div>
                <div className="mb-1 text-[0.8rem] font-semibold">
                  Retrieved memory ({snap.retrieval_result.retrieved_count})
                </div>
                <div className="space-y-1">
                  {snap.retrieval_result.entries.map((e, i) => (
                    <div
                      key={i}
                      className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-2.5 py-1.5 text-[0.78rem]"
                    >
                      <div className="flex items-center gap-1.5">
                        <Badge color="blue">{e.memory_type}</Badge>
                        {e.match_reason && <span className="text-[0.7rem] text-[var(--color-fg-subtle)]">{e.match_reason}</span>}
                        {typeof e.score === "number" && (
                          <span className="text-[0.7rem] text-[var(--color-fg-subtle)]">score {e.score}</span>
                        )}
                      </div>
                      <div className="mt-0.5 text-[var(--color-fg-muted)]">{e.content}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div>
              <div className="mb-1 text-[0.8rem] font-semibold">Assembled payload (exact messages sent)</div>
              <JsonView data={snap.assembled_payload} maxHeight="360px" />
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
