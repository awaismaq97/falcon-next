"use client";

import { useEffect, useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { LogOut, Menu, Shield } from "lucide-react";
import { useConfig } from "@/lib/queries";
import { useSettings } from "@/lib/store";
import { useAuth } from "@/lib/authStore";
import { cn } from "@/lib/utils";
import { Sidebar } from "@/components/Sidebar";
import { Button, Spinner } from "@/components/ui/primitives";
import { ChatTab } from "@/components/tabs/ChatTab";
import { CategoriesTab } from "@/components/tabs/CategoriesTab";
import { ContextTab } from "@/components/tabs/ContextTab";
import { MemoryTab } from "@/components/tabs/MemoryTab";
import { AuditTab } from "@/components/tabs/AuditTab";
import { LogsTab } from "@/components/tabs/LogsTab";
import { TestingTab } from "@/components/tabs/TestingTab";
import { DualRunTab } from "@/components/tabs/DualRunTab";
import { PolyMarketTab } from "@/components/tabs/PolyMarketTab";
import { KalshiTab } from "@/components/tabs/KalshiTab";
import { AdminPanel } from "@/components/admin/AdminPanel";

const ALL_TABS = [
  { id: "chat",        label: "Chat" },
  { id: "context",     label: "Context" },
  { id: "memory",      label: "Memory" },
  { id: "categories",  label: "Categories" },
  { id: "audit",       label: "Audit" },
  { id: "logs",        label: "Logs" },
  { id: "testing",     label: "Testing" },
  { id: "dualrun",     label: "Dual Run" },
  { id: "polymarket",  label: "Poly Market" },
  { id: "kalshi",      label: "Kalshi" },
];

export default function Home() {
  const { data: config, isLoading, isError, error } = useConfig();
  const { initFromConfig, activeTab, setActiveTab } = useSettings();
  const { user, logout } = useAuth();

  // Admins (features === null) see all tabs. Portal users only see enabled ones.
  const TABS = ALL_TABS.filter(
    (t) => user?.features == null || user.features[t.id] !== false
  );
  const [dark, setDark] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [adminOpen, setAdminOpen] = useState(false);
  const [confirmLogout, setConfirmLogout] = useState(false);

  useEffect(() => {
    setDark(document.documentElement.classList.contains("dark"));
  }, []);

  useEffect(() => {
    if (config) initFromConfig(config);
  }, [config, initFromConfig]);

  function toggleDark() {
    setDark((d) => {
      const next = !d;
      const el = document.documentElement;
      el.classList.toggle("dark", next);
      el.style.colorScheme = next ? "dark" : "light";
      localStorage.setItem("falcon-theme", next ? "dark" : "light");
      return next;
    });
  }

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center gap-3 text-[var(--color-fg-subtle)]">
        <Spinner /> Loading Falcon…
      </div>
    );
  }
  if (isError) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-2 p-8 text-center">
        <div className="text-[var(--color-red)] font-semibold">Cannot reach the Falcon backend</div>
        <div className="max-w-md text-sm text-[var(--color-fg-muted)]">{(error as Error)?.message}</div>
        <div className="mt-2 text-xs text-[var(--color-fg-subtle)]">
          Is the FastAPI server running? Check NEXT_PUBLIC_API_BASE.
        </div>
        <button
          onClick={logout}
          className="mt-4 flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs text-[var(--color-fg-muted)] border border-[var(--color-border)] hover:bg-[var(--color-surface)] transition-colors"
        >
          <LogOut className="h-3.5 w-3.5" /> Log out
        </button>
      </div>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden bg-[var(--color-bg)]">
      {/* Sidebar — drawer on all screen sizes, toggled by the Menu button */}
      <div
        className={cn(
          "fixed inset-y-0 left-0 z-40 transition-transform",
          sidebarOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <Sidebar dark={dark} onToggleDark={toggleDark} />
      </div>
      {sidebarOpen && (
        <div className="fixed inset-0 z-30 bg-black/30" onClick={() => setSidebarOpen(false)} />
      )}

      {/* Admin panel modal */}
      {adminOpen && <AdminPanel onClose={() => setAdminOpen(false)} />}

      {/* Main */}
      <main className="flex min-w-0 flex-1 flex-col">
        <Tabs.Root value={activeTab} onValueChange={setActiveTab} className="flex min-h-0 flex-1 flex-col">
          <div className="flex items-center gap-2 border-b border-[var(--color-border)] px-2">
            {/* Hamburger */}
            <Button size="icon" variant="ghost" onClick={() => setSidebarOpen((o) => !o)}>
              <Menu className="h-4 w-4" />
            </Button>

            {/* Tabs */}
            <Tabs.List className="flex flex-1 overflow-x-auto">
              {TABS.map((t) => (
                <Tabs.Trigger
                  key={t.id}
                  value={t.id}
                  className={cn(
                    "shrink-0 border-b-2 border-transparent px-4 py-2.5 text-[0.85rem] font-medium text-[var(--color-fg-muted)] transition-colors",
                    "hover:text-[var(--color-fg)]",
                    "data-[state=active]:border-[var(--color-fg)] data-[state=active]:text-[var(--color-fg)]",
                  )}
                >
                  {t.label}
                </Tabs.Trigger>
              ))}
            </Tabs.List>

            {/* Right side: user badge + admin btn + logout */}
            <div className="ml-auto flex shrink-0 items-center gap-1.5 pl-2">
              {user && (
                <span className="hidden sm:block text-[0.72rem] text-[var(--color-fg-subtle)] font-mono">
                  {user.username}
                </span>
              )}
              {user?.role === "admin" && (
                <Button
                  size="icon"
                  variant="ghost"
                  onClick={() => setAdminOpen(true)}
                  title="Admin panel"
                >
                  <Shield className="h-4 w-4" />
                </Button>
              )}

              {/* Logout — shows inline confirm before actually logging out */}
              {confirmLogout ? (
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
                    Cancel
                  </button>
                </div>
              ) : (
                <Button
                  size="icon"
                  variant="ghost"
                  onClick={() => setConfirmLogout(true)}
                  title="Log out"
                >
                  <LogOut className="h-4 w-4" />
                </Button>
              )}
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-hidden">
            <Tabs.Content value="chat" className="h-full data-[state=inactive]:hidden" forceMount>
              <ChatTab />
            </Tabs.Content>
            <Tabs.Content value="context" className="h-full overflow-y-auto data-[state=inactive]:hidden">
              <ContextTab />
            </Tabs.Content>
            <Tabs.Content value="memory" className="h-full overflow-y-auto data-[state=inactive]:hidden">
              <MemoryTab />
            </Tabs.Content>
            <Tabs.Content value="categories" className="h-full overflow-hidden data-[state=inactive]:hidden">
              <CategoriesTab />
            </Tabs.Content>
            <Tabs.Content value="audit" className="h-full overflow-y-auto data-[state=inactive]:hidden">
              <AuditTab />
            </Tabs.Content>
            <Tabs.Content value="logs" className="h-full overflow-hidden data-[state=inactive]:hidden">
              <LogsTab />
            </Tabs.Content>
            <Tabs.Content value="testing" className="h-full overflow-y-auto data-[state=inactive]:hidden">
              <TestingTab />
            </Tabs.Content>
            <Tabs.Content value="dualrun" className="h-full overflow-y-auto data-[state=inactive]:hidden">
              <DualRunTab />
            </Tabs.Content>
            <Tabs.Content value="polymarket" className="h-full data-[state=inactive]:hidden">
              <PolyMarketTab />
            </Tabs.Content>
            <Tabs.Content value="kalshi" className="h-full data-[state=inactive]:hidden">
              <KalshiTab />
            </Tabs.Content>
          </div>
        </Tabs.Root>
      </main>
    </div>
  );
}
