"use client";

import { useState, useRef } from "react";
import { Eye, EyeOff, LogIn, AlertCircle } from "lucide-react";
import { useAuth } from "@/lib/authStore";
import { cn } from "@/lib/utils";

export function LoginPage() {
  const { login, isLoading, error, clearError } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [shake, setShake] = useState(false);

  // Track whether an error is currently displayed so we can clear it on the
  // next keystroke. We use a plain ref (not state) to avoid triggering an
  // extra re-render on every keystroke.
  const errorShown = useRef(false);

  // When the error is set, mark it as shown
  if (error) errorShown.current = true;

  function handleUsernameChange(v: string) {
    setUsername(v);
    if (errorShown.current) {
      errorShown.current = false;
      clearError();
    }
  }

  function handlePasswordChange(v: string) {
    setPassword(v);
    if (errorShown.current) {
      errorShown.current = false;
      clearError();
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!username.trim() || !password) return;
    setSubmitting(true);
    try {
      await login(username.trim(), password);
    } catch {
      // Error is already stored in authStore — trigger shake
      setShake(true);
      setTimeout(() => setShake(false), 600);
    } finally {
      setSubmitting(false);
    }
  }

  const busy = submitting || isLoading;
  const hasError = !!error;

  return (
    <div className="flex min-h-screen items-center justify-center bg-[var(--color-bg)] px-4">
      <div className="w-full max-w-sm">
        {/* Logo / Title */}
        <div className="mb-8 text-center">
          <div className="mb-3 text-5xl">🦅</div>
          <h1 className="text-2xl font-bold tracking-tight text-[var(--color-fg)]">
            Falcon
          </h1>
          <p className="mt-1 text-sm text-[var(--color-fg-subtle)]">
            Sign in to continue
          </p>
        </div>

        {/* Card */}
        <div
          className={cn(
            "rounded-xl border bg-[var(--color-surface)] p-8 shadow-sm transition-colors",
            hasError
              ? "border-red-400 dark:border-red-700"
              : "border-[var(--color-border)]",
            shake && "animate-[shake_0.5s_ease-in-out]",
          )}
        >
          <form onSubmit={handleSubmit} noValidate className="space-y-5">

            {/* ── Error banner ── */}
            {hasError && (
              <div
                role="alert"
                aria-live="assertive"
                className="flex items-start gap-2.5 rounded-lg border border-red-300 bg-red-50 px-3.5 py-3 dark:border-red-800 dark:bg-red-950/50"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-600 dark:text-red-400" />
                <p className="text-sm font-medium text-red-700 dark:text-red-300">
                  {error}
                </p>
              </div>
            )}

            {/* ── Username ── */}
            <div>
              <label
                htmlFor="falcon-username"
                className="mb-1.5 block text-sm font-medium text-[var(--color-fg-muted)]"
              >
                Username
              </label>
              <input
                id="falcon-username"
                type="text"
                autoComplete="username"
                autoFocus
                required
                disabled={busy}
                value={username}
                onChange={(e) => handleUsernameChange(e.target.value)}
                className={cn(
                  "w-full rounded-lg border bg-[var(--color-bg)] px-3.5 py-2.5 text-sm",
                  "text-[var(--color-fg)] placeholder:text-[var(--color-fg-subtle)]",
                  "outline-none transition-colors focus:ring-1",
                  hasError
                    ? "border-red-400 focus:border-red-500 focus:ring-red-300 dark:border-red-700"
                    : "border-[var(--color-border)] focus:border-[var(--color-fg-muted)] focus:ring-[var(--color-fg-muted)]/30",
                  "disabled:cursor-not-allowed disabled:opacity-50",
                )}
                placeholder="Enter your username"
              />
            </div>

            {/* ── Password ── */}
            <div>
              <label
                htmlFor="falcon-password"
                className="mb-1.5 block text-sm font-medium text-[var(--color-fg-muted)]"
              >
                Password
              </label>
              <div className="relative">
                <input
                  id="falcon-password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="current-password"
                  required
                  disabled={busy}
                  value={password}
                  onChange={(e) => handlePasswordChange(e.target.value)}
                  className={cn(
                    "w-full rounded-lg border bg-[var(--color-bg)] px-3.5 py-2.5 pr-10 text-sm",
                    "text-[var(--color-fg)] placeholder:text-[var(--color-fg-subtle)]",
                    "outline-none transition-colors focus:ring-1",
                    hasError
                      ? "border-red-400 focus:border-red-500 focus:ring-red-300 dark:border-red-700"
                      : "border-[var(--color-border)] focus:border-[var(--color-fg-muted)] focus:ring-[var(--color-fg-muted)]/30",
                    "disabled:cursor-not-allowed disabled:opacity-50",
                  )}
                  placeholder="Enter your password"
                />
                <button
                  type="button"
                  tabIndex={-1}
                  onClick={() => setShowPassword((v) => !v)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-[var(--color-fg-subtle)] hover:text-[var(--color-fg-muted)]"
                  aria-label={showPassword ? "Hide password" : "Show password"}
                >
                  {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </div>

            {/* ── Submit ── */}
            <button
              type="submit"
              disabled={busy || !username.trim() || !password}
              className={cn(
                "flex w-full items-center justify-center gap-2 rounded-lg px-4 py-2.5",
                "text-sm font-semibold transition-opacity",
                "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2",
                "disabled:cursor-not-allowed disabled:opacity-40",
                hasError
                  ? "bg-red-600 text-white hover:opacity-90 active:opacity-75"
                  : "bg-[var(--color-fg)] text-[var(--color-bg)] hover:opacity-90 active:opacity-75",
              )}
            >
              {busy ? (
                <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
              ) : (
                <LogIn className="h-4 w-4" />
              )}
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </form>
        </div>

        <p className="mt-6 text-center text-xs text-[var(--color-fg-subtle)]">
          Access restricted to authorized users only.
        </p>
      </div>

      {/* Shake keyframe */}
      <style>{`
        @keyframes shake {
          0%, 100% { transform: translateX(0); }
          15%       { transform: translateX(-6px); }
          30%       { transform: translateX(6px); }
          45%       { transform: translateX(-5px); }
          60%       { transform: translateX(5px); }
          75%       { transform: translateX(-3px); }
          90%       { transform: translateX(3px); }
        }
      `}</style>
    </div>
  );
}
