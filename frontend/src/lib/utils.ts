import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function shortModel(model: string): string {
  return model.split("/").pop() ?? model;
}

export function fmtNum(n: number | undefined | null): string {
  if (n == null) return "0";
  return n.toLocaleString();
}

export function fmtTime(iso: string | undefined): string {
  if (!iso) return "";
  // ISO like 2026-07-06T15:42:34Z → 15:42:34 · 2026-07-06
  const m = iso.match(/(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/);
  if (!m) return iso;
  return `${m[2]} · ${m[1]}`;
}

/**
 * Flatten Markdown to clean prose for text-to-speech, so the model's `**bold**`,
 * `# headings`, code fences, and `[links](url)` aren't read out as punctuation.
 * This is deliberately lightweight (regex, no parser) — good enough for speech.
 */
export function plainTextForSpeech(md: string): string {
  let t = md ?? "";
  t = t.replace(/```[\s\S]*?```/g, " "); // fenced code blocks
  t = t.replace(/`([^`]+)`/g, "$1"); // inline code
  t = t.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1"); // images → alt
  t = t.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1"); // links → text
  t = t.replace(/^\s{0,3}#{1,6}\s+/gm, ""); // headings
  t = t.replace(/^\s{0,3}>\s?/gm, ""); // blockquotes
  t = t.replace(/^\s*[-*+]\s+/gm, ""); // bullet markers
  t = t.replace(/^\s*\d+\.\s+/gm, ""); // numbered markers
  t = t.replace(/(\*\*|__)(.*?)\1/g, "$2"); // bold
  t = t.replace(/(\*|_)(.*?)\1/g, "$2"); // italic
  t = t.replace(/~~(.*?)~~/g, "$1"); // strikethrough
  t = t.replace(/\|/g, " "); // table pipes
  t = t.replace(/\n{2,}/g, "\n").replace(/[ \t]{2,}/g, " "); // collapse whitespace
  return t.trim();
}

export function downloadJSON(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  triggerDownload(filename, blob);
}

export function downloadText(filename: string, text: string, mime = "text/markdown") {
  triggerDownload(filename, new Blob([text], { type: mime }));
}

function triggerDownload(filename: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
