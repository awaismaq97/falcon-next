"use client";

import { useState } from "react";
import { Users, ScrollText, LogOut, Shield, X } from "lucide-react";
import { useAuth } from "@/lib/authStore";
import { cn } from "@/lib/utils";
import { UsersTab } from "./UsersTab";
import { AuditLogTab } from "./AuditLogTab";

type AdminTab = "users" | "audit";

interface AdminPanelProps {
  onClose: () => void;
}

export function AdminPanel({ onClose }: AdminPanelProps) {
  const [activeTab, setActiveTab] = useState<AdminTab>("users");
  const { user, logout } = useAuth();
  const [confirmLogout, setConfirmLogout] = useState(false);

  return (
    /* Full-screen on mobile, centred modal on larger screens */
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/50 backdrop-blur-sm">
      <div
        className={cn(
          "flex flex-col bg-[var(--color-bg)] shadow-2xl",
          "w-full border border-[var(--color-border)]",
          // Mobile: slide up from bottom, full width, 95% height
          "h-[95dvh] rounded-t-2xl",
          // sm+: centred modal
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
            <h2 className="text-sm font-semibold text-[var(--color-fg)] truncate">
              Admin Panel
            </h2>
            <span className="hidden xs:inline-block rounded-full bg-[var(--color-surface)] px-2 py-0.5 text-xs text-[var(--color-fg-muted)] border border-[var(--color-border)] truncate max-w-[120px]">
              {user?.username}
            </span>
          </div>

          <div className="flex shrink-0 items-center gap-1.5">
            {confirmLogout ? (
              /* Inline confirm — compact on mobile */
              <div className="flex items-center gap-1 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1">
                <span className="text-xs text-[var(--color-fg-muted)]">Log out?</span>
                <button
                  onClick={logout}
                  className="rounded px-2 py-0.5 text-xs font-semibold text-red-600 hover:bg-red-50 dark:hover:bg-red-950/40 transition-colors"
                  autoFocus
                >
                  Yes
                </button>
                <button
                  onClick={() => setConfirmLogout(false)}
                  className="rounded px-2 py-0.5 text-xs text-[var(--color-fg-muted)] hover:bg-[var(--color-bg)] transition-colors"
                >
                  No
                </button>
              </div>
            ) : (
              <button
                onClick={() => setConfirmLogout(true)}
                className="flex items-center gap-1 rounded-lg px-2 py-1.5 text-xs text-[var(--color-fg-muted)] hover:bg-[var(--color-surface)] hover:text-[var(--color-fg)] transition-colors"
                title="Log out"
              >
                <LogOut className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">Log out</span>
              </button>
            )}
            <button
              onClick={onClose}
              className="rounded-lg p-1.5 text-[var(--color-fg-muted)] hover:bg-[var(--color-surface)] hover:text-[var(--color-fg)] transition-colors"
              aria-label="Close admin panel"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* ── Tab bar ── */}
        <div className="flex shrink-0 border-b border-[var(--color-border)] px-2 sm:px-6">
          <TabButton
            active={activeTab === "users"}
            onClick={() => setActiveTab("users")}
            icon={<Users className="h-4 w-4" />}
            label="Users"
          />
          <TabButton
            active={activeTab === "audit"}
            onClick={() => setActiveTab("audit")}
            icon={<ScrollText className="h-4 w-4" />}
            label="Audit Log"
          />
        </div>

        {/* ── Content ── */}
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
          {activeTab === "users" && <UsersTab />}
          {activeTab === "audit" && <AuditLogTab />}
        </div>
      </div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex items-center gap-2 border-b-2 px-4 py-3 text-sm font-medium transition-colors",
        active
          ? "border-[var(--color-fg)] text-[var(--color-fg)]"
          : "border-transparent text-[var(--color-fg-muted)] hover:text-[var(--color-fg)]",
      )}
    >
      {icon}
      {label}
    </button>
  );
}
