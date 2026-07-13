"use client";

import { useEffect, useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { Menu } from "lucide-react";
import { useConfig } from "@/lib/queries";
import { useSettings } from "@/lib/store";
import { cn } from "@/lib/utils";
import { Sidebar } from "@/components/Sidebar";
import { Button, Spinner } from "@/components/ui/primitives";
import { ChatTab } from "@/components/tabs/ChatTab";
import { ContextTab } from "@/components/tabs/ContextTab";
import { MemoryTab } from "@/components/tabs/MemoryTab";
import { AuditTab } from "@/components/tabs/AuditTab";
import { LogsTab } from "@/components/tabs/LogsTab";
import { TestingTab } from "@/components/tabs/TestingTab";
import { DualRunTab } from "@/components/tabs/DualRunTab";
import { PolyMarketTab } from "@/components/tabs/PolyMarketTab";
import { KalshiTab } from "@/components/tabs/KalshiTab";

const TABS = [
  { id: "chat", label: "Chat" },
  { id: "context", label: "Context" },
  { id: "memory", label: "Memory" },
  { id: "audit", label: "Audit" },
  { id: "logs", label: "Logs" },
  { id: "testing", label: "Testing" },
  { id: "dualrun", label: "Dual Run" },
  { id: "polymarket", label: "Poly Market" },
  { id: "kalshi", label: "Kalshi" },
];

export default function Home() {
  const { data: config, isLoading, isError, error } = useConfig();
  const { initFromConfig, activeTab, setActiveTab } = useSettings();
  const [dark, setDark] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // The theme class is already applied pre-paint by the inline script in the
  // root layout; mirror that state so the toggle icon matches on first render.
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

      {/* Main */}
      <main className="flex min-w-0 flex-1 flex-col">
        <Tabs.Root value={activeTab} onValueChange={setActiveTab} className="flex min-h-0 flex-1 flex-col">
          <div className="flex items-center gap-2 border-b border-[var(--color-border)] px-2">
            <Button size="icon" variant="ghost" onClick={() => setSidebarOpen((o) => !o)}>
              <Menu className="h-4 w-4" />
            </Button>
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
