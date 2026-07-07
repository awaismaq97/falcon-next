"use client";

// Lightweight, dependency-free toast system.
//
// A module-level store lets any module call `toast.error(...)` / `toast.success(...)`
// without a hook or prop-drilling — the <Toaster/> mounted once in providers.tsx
// subscribes and renders. Replaces blocking window.alert() everywhere.

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

type ToastKind = "error" | "success" | "info";
export interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

let counter = 0;
let items: ToastItem[] = [];
const listeners = new Set<(items: ToastItem[]) => void>();

function emit() {
  for (const l of listeners) l(items);
}

function dismiss(id: number) {
  items = items.filter((t) => t.id !== id);
  emit();
}

function push(kind: ToastKind, message: string, ttl: number) {
  const id = ++counter;
  items = [...items, { id, kind, message }];
  emit();
  if (ttl > 0) setTimeout(() => dismiss(id), ttl);
  return id;
}

/** Fire a toast from anywhere — no hook required. */
export const toast = {
  error: (m: string) => push("error", m, 6000),
  success: (m: string) => push("success", m, 3500),
  info: (m: string) => push("info", m, 4000),
  dismiss,
};

const KIND_CLASS: Record<ToastKind, string> = {
  error:
    "border-red-200 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/70 dark:text-red-200",
  success:
    "border-green-200 bg-green-50 text-green-800 dark:border-green-900 dark:bg-green-950/70 dark:text-green-200",
  info: "border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-fg)]",
};

export function Toaster() {
  const [list, setList] = useState<ToastItem[]>(items);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
    listeners.add(setList);
    setList(items);
    return () => {
      listeners.delete(setList);
    };
  }, []);

  if (!mounted) return null;

  return createPortal(
    <div className="pointer-events-none fixed inset-x-0 top-3 z-[100] flex flex-col items-center gap-2 px-3 sm:inset-x-auto sm:right-4 sm:items-end">
      {list.map((t) => (
        <div
          key={t.id}
          role={t.kind === "error" ? "alert" : "status"}
          aria-live={t.kind === "error" ? "assertive" : "polite"}
          className={cn(
            "toast-in pointer-events-auto flex w-full max-w-sm items-start gap-2.5 rounded-xl border px-3.5 py-2.5 text-[0.83rem] leading-snug shadow-lg",
            KIND_CLASS[t.kind],
          )}
        >
          <span className="min-w-0 flex-1 break-words">{t.message}</span>
          <button
            onClick={() => dismiss(t.id)}
            aria-label="Dismiss"
            className="-mr-1 mt-px shrink-0 rounded p-0.5 opacity-60 transition-opacity hover:opacity-100"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
    </div>,
    document.body,
  );
}
