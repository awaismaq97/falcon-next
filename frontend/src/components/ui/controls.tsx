"use client";

import * as React from "react";
import * as SwitchPrimitive from "@radix-ui/react-switch";
import * as SliderPrimitive from "@radix-ui/react-slider";
import * as SelectPrimitive from "@radix-ui/react-select";
import { Check, ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

// ── Switch ──────────────────────────────────────────────────────────────────
export function Switch({
  checked,
  onCheckedChange,
  id,
}: {
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
  id?: string;
}) {
  return (
    <SwitchPrimitive.Root
      id={id}
      checked={checked}
      onCheckedChange={onCheckedChange}
      className={cn(
        "relative inline-flex h-[20px] w-[36px] shrink-0 cursor-pointer items-center rounded-full transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-fg)]/20",
        "data-[state=checked]:bg-[var(--color-blue)] data-[state=unchecked]:bg-[var(--color-border-strong)]",
      )}
    >
      <SwitchPrimitive.Thumb className="pointer-events-none block h-[15px] w-[15px] translate-x-[3px] rounded-full bg-white shadow transition-transform data-[state=checked]:translate-x-[18px]" />
    </SwitchPrimitive.Root>
  );
}

export function Toggle({
  label,
  checked,
  onCheckedChange,
  hint,
}: {
  label: string;
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
  hint?: string;
}) {
  return (
    <div className="flex items-center justify-between gap-3 py-0.5">
      <div className="min-w-0">
        <div className="text-[0.85rem] text-[var(--color-fg)]">{label}</div>
        {hint && <div className="text-[0.72rem] text-[var(--color-fg-subtle)] leading-snug">{hint}</div>}
      </div>
      <Switch checked={checked} onCheckedChange={onCheckedChange} />
    </div>
  );
}

// ── Slider ──────────────────────────────────────────────────────────────────
export function Slider({
  value,
  min,
  max,
  step,
  onValueChange,
}: {
  value: number;
  min: number;
  max: number;
  step: number;
  onValueChange: (v: number) => void;
}) {
  return (
    <SliderPrimitive.Root
      className="relative flex h-5 w-full touch-none select-none items-center"
      value={[value]}
      min={min}
      max={max}
      step={step}
      onValueChange={(v) => onValueChange(v[0])}
    >
      <SliderPrimitive.Track className="relative h-1 w-full grow rounded-full bg-[var(--color-border-strong)]">
        <SliderPrimitive.Range className="absolute h-full rounded-full bg-[var(--color-accent)]" />
      </SliderPrimitive.Track>
      <SliderPrimitive.Thumb className="block h-3.5 w-3.5 rounded-full border-2 border-[var(--color-accent)] bg-[var(--color-bg)] shadow focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-fg)]/20" />
    </SliderPrimitive.Root>
  );
}

// ── Select ──────────────────────────────────────────────────────────────────
export interface SelectOption {
  value: string;
  /** Primary text — shown in the collapsed trigger (truncated) and the dropdown. */
  label: string;
  /** Optional secondary text — shown only in the open dropdown, not the trigger. */
  hint?: string;
}

export function Select({
  value,
  onValueChange,
  options,
  placeholder,
  className,
}: {
  value: string;
  onValueChange: (v: string) => void;
  options: SelectOption[];
  placeholder?: string;
  className?: string;
}) {
  return (
    <SelectPrimitive.Root value={value} onValueChange={onValueChange}>
      <SelectPrimitive.Trigger
        className={cn(
          "inline-flex h-9 w-full min-w-0 items-center justify-between gap-2 rounded-lg border border-[var(--color-border-strong)] bg-[var(--color-bg)] px-3 text-[0.85rem] text-[var(--color-fg)]",
          "focus:outline-none focus:border-[var(--color-fg)] focus:ring-2 focus:ring-[var(--color-fg)]/10 data-[placeholder]:text-[var(--color-fg-subtle)]",
          className,
        )}
      >
        {/* Truncate to a single line so long labels never spill outside the box. */}
        <span className="min-w-0 flex-1 truncate text-left">
          <SelectPrimitive.Value placeholder={placeholder} />
        </span>
        <SelectPrimitive.Icon className="shrink-0">
          <ChevronDown className="h-4 w-4 text-[var(--color-fg-subtle)]" />
        </SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          position="popper"
          sideOffset={4}
          className="z-50 max-h-[300px] overflow-hidden rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] shadow-lg"
        >
          <SelectPrimitive.Viewport className="p-1">
            {options.map((opt) => (
              <SelectPrimitive.Item
                key={opt.value}
                value={opt.value}
                className="relative flex min-h-8 cursor-pointer select-none items-start rounded-md py-1.5 pl-7 pr-3 text-[0.83rem] text-[var(--color-fg)] outline-none data-[highlighted]:bg-[var(--color-surface-2)]"
              >
                <SelectPrimitive.ItemIndicator className="absolute left-2 top-2 inline-flex items-center">
                  <Check className="h-3.5 w-3.5" />
                </SelectPrimitive.ItemIndicator>
                <span className="flex min-w-0 flex-col">
                  <SelectPrimitive.ItemText>{opt.label}</SelectPrimitive.ItemText>
                  {opt.hint && (
                    <span className="text-[0.7rem] leading-tight text-[var(--color-fg-subtle)]">{opt.hint}</span>
                  )}
                </span>
              </SelectPrimitive.Item>
            ))}
          </SelectPrimitive.Viewport>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  );
}
