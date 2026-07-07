"use client";

import * as React from "react";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
type ButtonSize = "sm" | "md" | "icon";

const VARIANTS: Record<ButtonVariant, string> = {
  primary:
    "bg-[var(--color-accent)] text-[var(--color-bg)] hover:opacity-90 border border-transparent",
  secondary:
    "bg-[var(--color-bg)] text-[var(--color-fg)] border border-[var(--color-border-strong)] hover:bg-[var(--color-surface-2)] hover:border-[var(--color-fg)]",
  ghost:
    "bg-transparent text-[var(--color-fg-muted)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-fg)] border border-transparent",
  danger:
    "bg-transparent text-[var(--color-fg-subtle)] border border-[var(--color-border)] hover:border-[var(--color-red)] hover:text-[var(--color-red)]",
};
const SIZES: Record<ButtonSize, string> = {
  sm: "h-8 px-3 text-[0.8rem] gap-1.5",
  md: "h-9 px-4 text-[0.85rem] gap-2",
  icon: "h-8 w-8 justify-center",
};

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = "secondary", size = "md", loading, children, disabled, ...props }, ref) => (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={cn(
        "inline-flex items-center rounded-lg font-medium transition-colors select-none",
        "disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-fg)]/20",
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...props}
    >
      {loading && <Loader2 className="h-3.5 w-3.5 spin" />}
      {children}
    </button>
  ),
);
Button.displayName = "Button";

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        "w-full h-9 rounded-lg border border-[var(--color-border-strong)] bg-[var(--color-bg)] px-3 text-[0.85rem]",
        "text-[var(--color-fg)] placeholder:text-[var(--color-fg-subtle)]",
        "focus:outline-none focus:border-[var(--color-fg)] focus:ring-2 focus:ring-[var(--color-fg)]/10",
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = "Input";

export const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => (
  <textarea
    ref={ref}
    className={cn(
      "w-full rounded-lg border border-[var(--color-border-strong)] bg-[var(--color-bg)] px-3 py-2 text-[0.85rem] leading-relaxed",
      "text-[var(--color-fg)] placeholder:text-[var(--color-fg-subtle)] resize-y",
      "focus:outline-none focus:border-[var(--color-fg)] focus:ring-2 focus:ring-[var(--color-fg)]/10",
      className,
    )}
    {...props}
  />
));
Textarea.displayName = "Textarea";

export function Badge({
  children,
  className,
  color,
}: {
  children: React.ReactNode;
  className?: string;
  color?: "green" | "red" | "amber" | "blue" | "gray";
}) {
  const colors: Record<string, string> = {
    green: "bg-green-50 text-green-700 border-green-200 dark:bg-green-950/40 dark:text-green-400 dark:border-green-900",
    red: "bg-red-50 text-red-700 border-red-200 dark:bg-red-950/40 dark:text-red-400 dark:border-red-900",
    amber: "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/40 dark:text-amber-400 dark:border-amber-900",
    blue: "bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-950/40 dark:text-blue-400 dark:border-blue-900",
    gray: "bg-[var(--color-surface-2)] text-[var(--color-fg-muted)] border-[var(--color-border)]",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[0.7rem] font-medium",
        colors[color ?? "gray"],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("h-4 w-4 spin text-[var(--color-fg-subtle)]", className)} />;
}

export function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[0.66rem] font-semibold tracking-[0.08em] uppercase text-[var(--color-fg-muted)] mb-1.5 mt-3">
      {children}
    </div>
  );
}

export function Card({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        "rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-[0.75rem] text-[var(--color-fg-muted)] mb-1">{label}</span>
      {children}
    </label>
  );
}
