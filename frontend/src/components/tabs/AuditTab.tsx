"use client";

import { useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Download, ChevronRight } from "lucide-react";
import { useState } from "react";
import { api } from "@/lib/api";
import { useAuditSummaries } from "@/lib/queries";
import { useSettings } from "@/lib/store";
import { Button, Badge, Spinner } from "@/components/ui/primitives";
import { JsonView } from "@/components/JsonView";
import { cn, downloadJSON, fmtTime, shortModel } from "@/lib/utils";

function AuditRow({ rec }: { rec: import("@/lib/types").AuditSummary }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(false);

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (next && !detail) {
      setLoading(true);
      try {
        setDetail(await api.auditDetail(rec._id));
      } finally {
        setLoading(false);
      }
    }
  }

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)]">
      <button onClick={toggle} className="flex w-full items-center gap-2 px-3 py-2 text-left">
        <ChevronRight className={cn("h-4 w-4 shrink-0 text-[var(--color-fg-subtle)] transition-transform", open && "rotate-90")} />
        <span className="font-mono text-[0.75rem] text-[var(--color-fg-subtle)]">{fmtTime(rec.recorded_at)}</span>
        <Badge color="gray">{shortModel(rec.model)}</Badge>
        <Badge color={rec.prompt_state === "present" ? "blue" : "gray"}>{rec.prompt_state}</Badge>
        <span className="ml-auto flex gap-3 text-[0.72rem] text-[var(--color-fg-subtle)]">
          <span>{rec.usage?.total_tokens ?? 0} tok</span>
          <span>{rec.latency_ms}ms</span>
          <span>ctx {rec.context_size}</span>
        </span>
      </button>
      {open && (
        <div className="border-t border-[var(--color-border)] p-3">
          {loading ? <Spinner /> : detail ? <JsonView data={detail} maxHeight="420px" /> : <span className="text-[0.8rem] text-[var(--color-fg-subtle)]">No detail.</span>}
        </div>
      )}
    </div>
  );
}

export function AuditTab() {
  const identityId = useSettings((s) => s.identityId);
  const { data, isLoading, isFetchingNextPage, hasNextPage, fetchNextPage } = useAuditSummaries(identityId);

  const records = data?.pages.flatMap((p) => p.records) ?? [];
  const total = data?.pages[0]?.total ?? 0;

  const scrollParentRef = useRef<HTMLDivElement>(null);
  const rowVirtualizer = useVirtualizer({
    count: records.length,
    getScrollElement: () => scrollParentRef.current,
    estimateSize: () => 48,
    overscan: 6,
    paddingStart: 8,
    paddingEnd: 8,
  });

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="shrink-0 border-b border-[var(--color-border)] px-5 py-4">
        <div className="mx-auto flex max-w-4xl items-center justify-between gap-2">
          <div className="min-w-0">
            <h2 className="text-[1rem] font-semibold">Audit trail</h2>
            <p className="break-words text-[0.78rem] text-[var(--color-fg-subtle)]">
              Every inference event for <span className="font-mono">{identityId}</span>
              {total > 0 && <span className="ml-1 text-[var(--color-fg-muted)]">· {total} total</span>}
            </p>
          </div>
          <Button
            size="sm"
            variant="secondary"
            className="shrink-0"
            onClick={async () => downloadJSON(`falcon_audit_${identityId}.json`, await api.exportAudit(identityId))}
          >
            <Download className="h-3.5 w-3.5" /> Export
          </Button>
        </div>
      </div>

      {/* Scroll area */}
      <div ref={scrollParentRef} className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl px-5">
          {isLoading ? (
            <div className="flex items-center gap-2 py-6 text-[var(--color-fg-subtle)]">
              <Spinner /> Loading audit summaries…
            </div>
          ) : records.length === 0 ? (
            <p className="py-6 text-[0.85rem] text-[var(--color-fg-subtle)]">No audit records yet.</p>
          ) : (
            <>
              <div style={{ height: rowVirtualizer.getTotalSize(), position: "relative" }}>
                {rowVirtualizer.getVirtualItems().map((vi) => (
                  <div
                    key={vi.key}
                    data-index={vi.index}
                    ref={rowVirtualizer.measureElement}
                    style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${vi.start}px)` }}
                  >
                    <div className="pb-1.5">
                      <AuditRow rec={records[vi.index]} />
                    </div>
                  </div>
                ))}
              </div>

              {/* Load more */}
              {hasNextPage && (
                <div className="py-4 text-center">
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => fetchNextPage()}
                    loading={isFetchingNextPage}
                  >
                    Load more
                  </Button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
