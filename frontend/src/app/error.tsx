"use client";

// Route-level error boundary. Catches render/runtime errors thrown anywhere in
// the page tree and shows a recoverable UI instead of a blank screen.

import { useEffect } from "react";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <div className="flex h-screen flex-col items-center justify-center gap-3 p-8 text-center">
      <div className="text-4xl">🦅</div>
      <div className="text-lg font-semibold text-[var(--color-red)]">Something went wrong</div>
      <div className="max-w-md text-sm text-[var(--color-fg-muted)]">
        {error.message || "An unexpected error occurred while rendering this view."}
      </div>
      <button
        onClick={reset}
        className="mt-2 rounded-lg border border-[var(--color-border-strong)] bg-[var(--color-bg)] px-4 py-2 text-[0.85rem] font-medium text-[var(--color-fg)] transition-colors hover:bg-[var(--color-surface-2)] hover:border-[var(--color-fg)]"
      >
        Try again
      </button>
    </div>
  );
}
