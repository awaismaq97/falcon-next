"use client";

import { memo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

// Defined once at module scope so the plugin/component references are stable
// across renders (react-markdown re-parses when these change identity).
const REMARK_PLUGINS = [remarkGfm];
const COMPONENTS: Components = {
  a: ({ ...props }) => <a target="_blank" rel="noopener noreferrer" {...props} />,
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
