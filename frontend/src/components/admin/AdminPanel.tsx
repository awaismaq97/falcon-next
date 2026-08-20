"use client";

import { useState } from "react";
import { Users, ScrollText, LogOut, Shield, X, Bot } from "lucide-react";
import { useAuth } from "@/lib/authStore";
import { cn } from "@/lib/utils";
import { UsersTab } from "./UsersTab";
import { AuditLogTab } from "./AuditLogTab";
import { adminApi } from "@/lib/adminApi";
import { useWatcherStatus, qk } from "@/lib/queries";
import { useQueryClient } from "@tanstack/react-query";

type AdminTab = "users" | "audit" | "watcher";

interface AdminPanelProps {
  onClose: () => void;
}

// ---------------------------------------------------------------------------
// Admin's own watcher control
// ---------------------------------------------------------------------------
function AdminWatcherTab() {
  const { user } = useAuth();
  const identityId = user?.identity_id ?? "default";
  const qc = useQueryClient();
  const { data: status, isLoading } = useWatcherStatus(identityId);
  const [toggling, setToggling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastResponse, setLastResponse] = useState<string | null>(null);

  async function toggle() {
    setToggling(true);
    setError(null);
    setLastResponse(null);
    try {
      const result = await adminApi.setWatcherForIdentity(identityId, !status?.enabled);
      setLastResponse(JSON.stringify(result, null, 2));
      await qc.refetchQueries({ queryKey: qk.watcherStatus(identityId) });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setToggling(false);
    }
  }

  // Direct test: start watcher via fetch, show raw response
  async function directTest() {
    setError(null);
    setLastResponse(null);
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("falcon-auth-token") : null;
      const res = await fetch(`/api/identities/${encodeURIComponent(identityId)}/watcher/toggle`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ enabled: true }),
      });
      const text = await res.text();
      setLastResponse(`HTTP ${res.status}\n${text}`);
      await qc.refetchQueries({ queryKey: qk.watcherStatus(identityId) });
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const enabled = status?.enabled ?? false;
  const running = status?.running ?? false;

  return (
    <div className="p-6 max-w-lg space-y-4">
      <h3 className="text-sm font-semibold text-[var(--color-fg)]">Watcher Agent</h3>
      <p className="text-xs text-[var(--color-fg-muted)]">
        Monitors assistant messages for <span className="font-mono">[AGENT: cmd]...[/AGENT]</span> markers and executes them.
      </p>

      {/* Identity + status card */}
      <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3 space-y-1">
        <div className="flex items-center gap-2">
          <span className={cn(
            "h-2.5 w-2.5 rounded-full shrink-0",
            running ? "bg-green-500" : enabled ? "bg-amber-400 animate-pulse" : "bg-[var(--color-fg-subtle)]",
          )} />
          <span className="text-sm font-medium">
            {running ? "Running" : enabled ? "Enabled — starting" : "Disabled"}
          </span>
        </div>
        <div className="text-xs text-[var(--color-fg-muted)]">
          Identity: <span className="font-mono font-semibold">{identityId}</span>
        </div>
        <div className="text-xs text-[var(--color-fg-muted)]">
          User: <span className="font-mono">{user?.username ?? "—"}</span>
          {" · "}Role: <span className="font-mono">{user?.role ?? "—"}</span>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/40 px-3 py-2 text-xs text-red-700 dark:text-red-300 font-mono whitespace-pre-wrap">
          {error}
        </div>
      )}

      {lastResponse && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-2)] px-3 py-2 text-xs font-mono whitespace-pre-wrap text-[var(--color-fg)]">
          {lastResponse}
        </div>
      )}

      <div className="flex gap-2">
        {/* Main toggle */}
        <button
          onClick={toggle}
          disabled={toggling || isLoading}
          className={cn(
            "flex items-center gap-2 rounded-lg px-4 py-2.5 text-sm font-semibold transition-colors disabled:opacity-50",
            enabled
              ? "border border-red-300 text-red-600 hover:bg-red-50 dark:border-red-800 dark:hover:bg-red-950/40"
              : "bg-[var(--color-fg)] text-[var(--color-bg)] hover:opacity-90",
          )}
        >
          <Bot className="h-4 w-4" />
          {toggling ? "Updating…" : enabled ? "Disable Watcher" : "Enable Watcher"}
        </button>

        {/* Raw test button — bypasses adminApi, shows exact HTTP response */}
        <button
          onClick={directTest}
          className="rounded-lg border border-[var(--color-border)] px-3 py-2 text-xs text-[var(--color-fg-muted)] hover:bg-[var(--color-surface)] hover:text-[var(--color-fg)] transition-colors"
          title="Direct API test — shows raw HTTP response"
        >
          Raw test
        </button>
      </div>

      {enabled && (
        <p className="text-xs text-[var(--color-fg-subtle)]">
          Test: send a message, the AI should emit{" "}
          <span className="font-mono">[AGENT: ping][/AGENT]</span> (requires system prompt).
        </p>
      )}
    </div>
  );
}

export function AdminPanel({ onClose }: AdminPanelProps) {
  const [activeTab, setActiveTab] = useState<AdminTab>("users");
  const { user, logout } = useAuth();
  const [confirmLogout, setConfirmLogout] = useState(false);

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/50 backdrop-blur-sm">
      <div
        className={cn(
          "flex flex-col bg-[var(--color-bg)] shadow-2xl",
          "w-full border border-[var(--color-border)]",
          "h-[95dvh] rounded-t-2xl",
          "sm:h-[90vh] sm:max-w-5xl sm:rounded-xl sm:mx-4",
        )}
        role="dialog"
        aria-label="Admin Panel"
        aria-modal="true"
      >
        {/* ── Header ── */}
        <div className="flex shrink-0 items-center justify-between border-b border-[var(--color-border)] px-4 py-3 sm:px-6 sm:py-4">
          <div className="flex items-center gap-2 min-w-0">
            <Shield className="h-4 w-4 shrink-0 text-[var(--color-fg-muted)]" />
            <h2 className="text-sm font-semibold text-[var(--color-fg)] truncate">Admin Panel</h2>
            <span className="hidden xs:inline-block rounded-full bg-[var(--color-surface)] px-2 py-0.5 text-xs text-[var(--color-fg-muted)] border border-[var(--color-border)] truncate max-w-[120px]">
              {user?.username}
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {confirmLogout ? (
              <div className="flex items-center gap-1 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1">
                <span className="text-xs text-[var(--color-fg-muted)]">Log out?</span>
                <button onClick={logout} className="rounded px-2 py-0.5 text-xs font-semibold text-red-600 hover:bg-red-50 dark:hover:bg-red-950/40 transition-colors" autoFocus>Yes</button>
                <button onClick={() => setConfirmLogout(false)} className="rounded px-2 py-0.5 text-xs text-[var(--color-fg-muted)] hover:bg-[var(--color-bg)] transition-colors">No</button>
              </div>
            ) : (
              <button onClick={() => setConfirmLogout(true)} className="flex items-center gap-1 rounded-lg px-2 py-1.5 text-xs text-[var(--color-fg-muted)] hover:bg-[var(--color-surface)] hover:text-[var(--color-fg)] transition-colors" title="Log out">
                <LogOut className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">Log out</span>
              </button>
            )}
            <button onClick={onClose} className="rounded-lg p-1.5 text-[var(--color-fg-muted)] hover:bg-[var(--color-surface)] hover:text-[var(--color-fg)] transition-colors" aria-label="Close admin panel">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* ── Tab bar ── */}
        <div className="flex shrink-0 border-b border-[var(--color-border)] px-2 sm:px-6">
          <TabButton active={activeTab === "users"} onClick={() => setActiveTab("users")} icon={<Users className="h-4 w-4" />} label="Users" />
          <TabButton active={activeTab === "audit"} onClick={() => setActiveTab("audit")} icon={<ScrollText className="h-4 w-4" />} label="Audit Log" />
          <TabButton active={activeTab === "watcher"} onClick={() => setActiveTab("watcher")} icon={<Bot className="h-4 w-4" />} label="Watcher" />
        </div>

        {/* ── Content ── */}
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
          {activeTab === "users" && <UsersTab />}
          {activeTab === "audit" && <AuditLogTab />}
          {activeTab === "watcher" && <AdminWatcherTab />}
        </div>
      </div>
    </div>
  );
}

function TabButton({ active, onClick, icon, label }: { active: boolean; onClick: () => void; icon: React.ReactNode; label: string }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex items-center gap-2 border-b-2 px-4 py-3 text-sm font-medium transition-colors",
        active ? "border-[var(--color-fg)] text-[var(--color-fg)]" : "border-transparent text-[var(--color-fg-muted)] hover:text-[var(--color-fg)]",
      )}
    >
      {icon}
      {label}
    </button>
  );
}
