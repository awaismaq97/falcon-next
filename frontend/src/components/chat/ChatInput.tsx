"use client";

import { useRef, useState } from "react";
import { ArrowUp, Paperclip, X, Square, FileText, Loader2, AlertCircle } from "lucide-react";
import { api } from "@/lib/api";
import type { DocAttachment } from "@/lib/types";
import { Button } from "@/components/ui/primitives";
import { toast } from "@/components/ui/toast";

const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
const MAX_DOC_BYTES = 25 * 1024 * 1024;

// Document extensions the backend can extract text from.
const DOC_EXTS = [
  "pdf", "docx", "xlsx", "xlsm", "pptx", "csv", "tsv", "txt", "md", "markdown",
  "json", "xml", "html", "htm", "yaml", "yml", "log",
];
// Legacy binary formats — accepted in the picker so the user gets a clear
// "re-save as .docx/.xlsx" message from the server rather than silent rejection.
const LEGACY_EXTS = ["doc", "xls", "ppt"];

const ACCEPT = [
  ...IMAGE_TYPES,
  ...DOC_EXTS.map((e) => `.${e}`),
  ...LEGACY_EXTS.map((e) => `.${e}`),
].join(",");

type ImageAtt = { id: string; kind: "image"; file: File };
type DocAtt = {
  id: string;
  kind: "doc";
  file: File;
  status: "extracting" | "ready" | "error";
  text?: string;
  chars?: number;
  truncated?: boolean;
  error?: string;
};
type Attachment = ImageAtt | DocAtt;

let _seq = 0;
const nextId = () => `att-${++_seq}`;
const extOf = (name: string) => (name.includes(".") ? name.split(".").pop()!.toLowerCase() : "");

function AttachmentChip({ att, onRemove }: { att: Attachment; onRemove: () => void }) {
  const name = att.file.name;
  const short = name.length > 24 ? name.slice(0, 24) + "…" : name;
  return (
    <div className="flex items-center gap-1.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 text-[0.75rem]">
      {att.kind === "image" ? (
        <span>🖼</span>
      ) : att.status === "extracting" ? (
        <Loader2 className="h-3.5 w-3.5 spin text-[var(--color-fg-subtle)]" />
      ) : att.status === "error" ? (
        <AlertCircle className="h-3.5 w-3.5 text-[var(--color-red)]" />
      ) : (
        <FileText className="h-3.5 w-3.5 text-[var(--color-fg-muted)]" />
      )}
      <span title={name} className="max-w-[12rem] truncate">
        {short}
      </span>
      {att.kind === "doc" && att.status === "ready" && att.chars != null && (
        <span className="text-[var(--color-fg-subtle)]">
          {att.truncated ? "truncated" : `${att.chars.toLocaleString()} chars`}
        </span>
      )}
      {att.kind === "doc" && att.status === "error" && (
        <span className="text-[var(--color-red)]" title={att.error}>
          failed
        </span>
      )}
      <button
        onClick={onRemove}
        className="text-[var(--color-fg-subtle)] hover:text-[var(--color-red)]"
        title="Remove"
      >
        <X className="h-3 w-3" />
      </button>
    </div>
  );
}

export function ChatInput({
  onSend,
  onStop,
  streaming,
}: {
  onSend: (text: string, images: File[], docs: DocAttachment[]) => void;
  onStop: () => void;
  streaming: boolean;
}) {
  const [text, setText] = useState("");
  const [atts, setAtts] = useState<Attachment[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  function patchDoc(id: string, patch: Partial<DocAtt>) {
    setAtts((prev) => prev.map((a) => (a.id === id && a.kind === "doc" ? { ...a, ...patch } : a)));
  }

  async function extractDoc(att: DocAtt) {
    try {
      const r = await api.extractDocument(att.file);
      patchDoc(att.id, { status: "ready", text: r.text, chars: r.chars, truncated: r.truncated });
      if (r.truncated) toast.info(`"${att.file.name}" was long — using the first ~200k characters.`);
    } catch (e) {
      patchDoc(att.id, { status: "error", error: (e as Error).message });
      toast.error(`"${att.file.name}": ${(e as Error).message}`);
    }
  }

  function addFiles(list: FileList | null) {
    if (!list) return;
    const toAdd: Attachment[] = [];
    for (const f of Array.from(list)) {
      if (IMAGE_TYPES.includes(f.type)) {
        if (f.size > MAX_IMAGE_BYTES) {
          toast.error(`Skipped "${f.name}" — image larger than 8 MB.`);
          continue;
        }
        toAdd.push({ id: nextId(), kind: "image", file: f });
        continue;
      }
      const ext = extOf(f.name);
      if (!DOC_EXTS.includes(ext) && !LEGACY_EXTS.includes(ext)) {
        toast.error(`Skipped "${f.name}" — unsupported file type.`);
        continue;
      }
      if (f.size > MAX_DOC_BYTES) {
        toast.error(`Skipped "${f.name}" — larger than 25 MB.`);
        continue;
      }
      toAdd.push({ id: nextId(), kind: "doc", file: f, status: "extracting" });
    }
    if (toAdd.length) {
      setAtts((prev) => [...prev, ...toAdd]);
      for (const a of toAdd) if (a.kind === "doc") extractDoc(a);
    }
  }

  const docsExtracting = atts.some((a) => a.kind === "doc" && a.status === "extracting");
  const images = atts.filter((a): a is ImageAtt => a.kind === "image");
  const readyDocs = atts.filter((a): a is DocAtt => a.kind === "doc" && a.status === "ready");
  const hasSendable = text.trim().length > 0 || images.length > 0 || readyDocs.length > 0;

  function submit() {
    if (streaming) return;
    if (docsExtracting) {
      toast.error("Wait for documents to finish processing.");
      return;
    }
    if (!hasSendable) return;
    const docs: DocAttachment[] = readyDocs.map((d) => ({ filename: d.file.name, text: d.text! }));
    onSend(
      text,
      images.map((i) => i.file),
      docs,
    );
    setText("");
    setAtts([]);
    if (taRef.current) taRef.current.style.height = "auto";
  }

  return (
    <div className="border-t border-[var(--color-border)] bg-[var(--color-bg)] px-4 py-3">
      <div className="mx-auto max-w-3xl">
        {atts.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-2">
            {atts.map((a) => (
              <AttachmentChip key={a.id} att={a} onRemove={() => setAtts((prev) => prev.filter((x) => x.id !== a.id))} />
            ))}
          </div>
        )}
        <div className="flex items-end gap-2 rounded-2xl border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-3 py-2 focus-within:border-[var(--color-fg)] focus-within:ring-2 focus-within:ring-[var(--color-fg)]/10">
          <button
            onClick={() => fileRef.current?.click()}
            className="mb-0.5 shrink-0 rounded-md p-1.5 text-[var(--color-fg-subtle)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-fg)]"
            title="Attach image or document (PDF, Word, Excel, PowerPoint, text)"
          >
            <Paperclip className="h-4 w-4" />
          </button>
          <input
            ref={fileRef}
            type="file"
            accept={ACCEPT}
            multiple
            hidden
            onChange={(e) => {
              addFiles(e.target.files);
              e.target.value = "";
            }}
          />
          <textarea
            ref={taRef}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              e.target.style.height = "auto";
              e.target.style.height = Math.min(e.target.scrollHeight, 200) + "px";
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            rows={1}
            placeholder="Message…"
            className="max-h-[200px] flex-1 resize-none bg-transparent py-1.5 text-[0.93rem] text-[var(--color-fg)] outline-none placeholder:text-[var(--color-fg-subtle)]"
          />
          {streaming ? (
            <Button size="icon" variant="secondary" onClick={onStop} title="Stop" className="mb-0.5 shrink-0 rounded-full">
              <Square className="h-3.5 w-3.5" />
            </Button>
          ) : (
            <Button
              size="icon"
              variant="primary"
              onClick={submit}
              disabled={!hasSendable || docsExtracting}
              title={docsExtracting ? "Processing document…" : "Send"}
              className="mb-0.5 shrink-0 rounded-full"
            >
              {docsExtracting ? <Loader2 className="h-4 w-4 spin" /> : <ArrowUp className="h-4 w-4" />}
            </Button>
          )}
        </div>
        <div className="mt-1.5 text-center text-[0.68rem] text-[var(--color-fg-subtle)]">
          Falcon is a transparent inference layer — every component entering generation is visible.
        </div>
      </div>
    </div>
  );
}
