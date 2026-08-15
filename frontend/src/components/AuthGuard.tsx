"use client";

import { useEffect } from "react";
import { useAuth } from "@/lib/authStore";
import { LoginPage } from "./LoginPage";

interface AuthGuardProps {
  children: React.ReactNode;
}

export function AuthGuard({ children }: AuthGuardProps) {
  const { isAuthenticated, isLoading, initialize } = useAuth();

  // initialize() is idempotent — the store guards against running it twice,
  // so this effect is safe even if it fires more than once.
  useEffect(() => {
    initialize();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Initial startup check — show full-screen spinner until we know
  // whether there's a valid stored token.
  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-[var(--color-bg)]">
        <div className="flex items-center gap-3 text-[var(--color-fg-subtle)]">
          <span className="h-5 w-5 animate-spin rounded-full border-2 border-[var(--color-fg-subtle)] border-t-transparent" />
          <span className="text-sm">Loading…</span>
        </div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <LoginPage />;
  }

  return <>{children}</>;
}
