"use client";

import { memo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { API_BASE } from "@/lib/config";
import { cn } from "@/lib/utils";

// Defined once at module scope so the plugin/component references are stable
// across renders (react-markdown re-parses when these change identity).
const REMARK_PLUGINS = [remarkGfm];

// The watcher emits download links as relative /api/... paths, because it has no
// way to know the browser's origin. In production that is already correct (one
// ingress fronts both); in dev the API is on another port, so prefix it here.
// Doing it at render time keeps the stored message text origin-independent — the
// same history opens correctly on localhost and in production.
const COMPONENTS: Components = {
  a: ({ href, ...props }) => {
    const isApi = typeof href === "string" && href.startsWith("/api/");
    const isDownload = isApi && href.includes("/download");
    return (
      <a
        href={isApi ? `${API_BASE}${href}` : href}
        target="_blank"
        rel="noopener noreferrer"
        // Lets the browser save it under its real filename rather than
        // navigating; the server's Content-Disposition carries the name.
        {...(isDownload ? { download: "" } : {})}
        {...props}
      />
    );
  },
};

// Memoized: parsing the markdown AST is the single most expensive thing on the
// streaming path. With memo, only the message whose text actually changed
// re-parses — static history bubbles are skipped entirely on every token.
export const Markdown = memo(function Markdown({
  children,
  className,
}: {
  children: string;
  className?: string;
}) {
  return (
    <div className={cn("prose-chat", className)}>
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={COMPONENTS}>
        {children}
      </ReactMarkdown>
    </div>
  );
});
