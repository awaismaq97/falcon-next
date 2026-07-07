"use client";

import { useState } from "react";
import { Copy, Check } from "lucide-react";
import { cn } from "@/lib/utils";

export function JsonView({
  data,
  className,
  maxHeight = "400px",
  collapsed = false,
}: {
  data: unknown;
  className?: string;
  maxHeight?: string;
  collapsed?: boolean;
}) {
  const [open, setOpen] = useState(!collapsed);
  const [copied, setCopied] = useState(false);
  const text = JSON.stringify(data, null, 2);

  return (
    <div className={cn("relative rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-2)]", className)}>
      <div className="flex items-center justify-between border-b border-[var(--color-border)] px-2.5 py-1">
        <button
          onClick={() => setOpen((o) => !o)}
          className="text-[0.7rem] font-mono text-[var(--color-fg-subtle)] hover:text-[var(--color-fg)]"
        >
          {open ? "▾ JSON" : "▸ JSON"}
        </button>
        <button
          onClick={() => {
            navigator.clipboard.writeText(text).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1100);
            });
          }}
          className="rounded p-1 text-[var(--color-fg-subtle)] hover:text-[var(--color-fg)]"
        >
          {copied ? <Check className="h-3 w-3 text-[var(--color-green)]" /> : <Copy className="h-3 w-3" />}
        </button>
      </div>
      {open && (
        <pre
          className="overflow-auto px-3 py-2 font-mono text-[0.72rem] leading-relaxed text-[var(--color-fg)]"
          style={{ maxHeight }}
        >
          {text}
        </pre>
      )}
    </div>
  );
}
