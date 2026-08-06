"use client";

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  Plus,
  Trash2,
  ChevronLeft,
  ChevronRight,
  FolderOpen,
  MessageSquare,
  Tag,
  AlertTriangle,
  RefreshCw,
  X,
  Check,
  Download,
  Volume2,
  Square,
  Loader2,
} from "lucide-react";
import { api } from "@/lib/api";
import { useCategories, useCategoryMessages, qk } from "@/lib/queries";
import { useSettings } from "@/lib/store";
import { useTts } from "@/lib/tts";
import type { Category, CategoryMessage } from "@/lib/types";
import { Button, Input, Spinner } from "@/components/ui/primitives";
import { Markdown } from "@/components/Markdown";
import { cn } from "@/lib/utils";
import { toast } from "@/components/ui/toast";

const PAGE_SIZE = 20;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDateTime(iso: string): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

// Short date: "Aug 7, 14:32" — used in tight spaces on mobile
function formatDateTimeShort(iso: string): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

// ---------------------------------------------------------------------------
// Reusable error banner
// ---------------------------------------------------------------------------

function ErrorBanner({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-[0.82rem] text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <div className="min-w-0 flex-1 break-words">{message}</div>
      {onRetry && (
        <button
          onClick={onRetry}
          className="ml-2 shrink-0 rounded-md px-2 py-1 text-[0.75rem] font-medium hover:bg-red-100 dark:hover:bg-red-900/40"
          title="Retry"
        >
          <RefreshCw className="inline h-3 w-3" /> Retry
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Inline confirm row — used in both card types
// Renders: [label?] [Confirm btn] [Cancel btn]
// ---------------------------------------------------------------------------

function ConfirmRow({
  label,
  onConfirm,
  onCancel,
  confirming,
}: {
  label?: string;
  onConfirm: (e: React.MouseEvent) => void;
  onCancel: (e: React.MouseEvent) => void;
  confirming: boolean;
}) {
  return (
    <div
      className="flex items-center gap-1"
      onClick={(e) => e.stopPropagation()}
    >
      {label && (
        <span className="hidden text-[0.72rem] text-[var(--color-fg-muted)] sm:inline">
          {label}
        </span>
      )}
      <button
        onClick={onConfirm}
        disabled={confirming}
        className={cn(
          "flex items-center gap-1 rounded-md px-2 py-1 text-[0.72rem] font-medium transition-colors",
          "bg-red-500 text-white hover:bg-red-600 disabled:opacity-50",
        )}
        title="Confirm delete"
      >
        {confirming ? (
          <Spinner />
        ) : (
          <>
            <Check className="h-3 w-3" />
            <span className="hidden sm:inline">Yes</span>
          </>
        )}
      </button>
      <button
        onClick={onCancel}
        disabled={confirming}
        className="flex items-center gap-1 rounded-md px-2 py-1 text-[0.72rem] text-[var(--color-fg-muted)] transition-colors hover:bg-[var(--color-surface-2)] hover:text-[var(--color-fg)] disabled:opacity-50"
        title="Cancel"
      >
        <X className="h-3 w-3" />
        <span className="hidden sm:inline">No</span>
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Speak button — mirrors the one in ChatMessage.tsx, keyed by message id
// ---------------------------------------------------------------------------

function SpeakButton({ id, text }: { id: string; text: string }) {
  const playing = useTts((s) => s.playingId === id);
  const loading = useTts((s) => s.loadingId === id);
  const toggle  = useTts((s) => s.toggle);
  return (
    <button
      onClick={() => toggle(id, text)}
      className={cn(
        "inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[0.72rem] transition-colors",
        playing
          ? "text-[var(--color-accent)]"
          : "text-black hover:bg-[var(--color-surface-2)] hover:text-black dark:text-white dark:hover:text-white",
      )}
      title={playing ? "Stop" : loading ? "Synthesising…" : "Read aloud"}
    >
      {loading ? (
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
      ) : playing ? (
        <Square className="h-3 w-3 fill-current" />
      ) : (
        <Volume2 className="h-3.5 w-3.5" />
      )}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Message pair card
// ---------------------------------------------------------------------------

function MessagePairCard({
  msg,
  onDelete,
  canSpeak,
}: {
  msg: CategoryMessage;
  onDelete: () => Promise<void>;
  canSpeak: boolean;
}) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function handleDelete(e: React.MouseEvent) {
    e.stopPropagation();
    setDeleting(true);
    setDeleteError(null);
    try {
      await onDelete();
    } catch (err) {
      setDeleteError((err as Error).message);
      setConfirmDelete(false);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]">
      {/* Header: timestamps (wrap on mobile) + delete action (always visible) */}
      <div className="border-b border-[var(--color-border)] bg-[var(--color-surface-2)] px-3 py-2">
        {/* Top row: timestamps */}
        <div className="flex flex-wrap gap-1.5 text-[0.72rem] text-[var(--color-fg-subtle)]">
          <span
            className="inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-1.5 py-0.5"
            title="User message time"
          >
            👤{" "}
            <span className="hidden sm:inline">{formatDateTime(msg.user_ts || msg.recorded_at)}</span>
            <span className="sm:hidden">{formatDateTimeShort(msg.user_ts || msg.recorded_at)}</span>
          </span>
          <span
            className="inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-1.5 py-0.5"
            title="Assistant response time"
          >
            🦅{" "}
            <span className="hidden sm:inline">{formatDateTime(msg.asst_ts || msg.recorded_at)}</span>
            <span className="sm:hidden">{formatDateTimeShort(msg.asst_ts || msg.recorded_at)}</span>
          </span>
          {msg.hallucinated_category && (
            <span
              className="inline-flex items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-amber-700 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-400"
              title={`Classifier returned an unrecognised category; filed under "Other". Original: ${msg.hallucinated_category}`}
            >
              ⚠{" "}
              <span className="max-w-[120px] truncate sm:max-w-none">
                {msg.hallucinated_category.length > 30
                  ? msg.hallucinated_category.slice(0, 30) + "…"
                  : msg.hallucinated_category}
              </span>
            </span>
          )}
        </div>

        {/* Bottom row: delete action — always visible, floated right */}
        <div className="mt-1.5 flex justify-end">
          {confirmDelete ? (
            <ConfirmRow
              label="Delete?"
              onConfirm={handleDelete}
              onCancel={(e) => { e.stopPropagation(); setConfirmDelete(false); }}
              confirming={deleting}
            />
          ) : (
            <button
              onClick={() => setConfirmDelete(true)}
              className="flex items-center gap-1 rounded-md p-1.5 text-[0.72rem] text-[var(--color-fg-subtle)] transition-colors hover:bg-[var(--color-surface)] hover:text-[var(--color-red)]"
              title="Delete this message pair"
            >
              <Trash2 className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">Delete</span>
            </button>
          )}
        </div>
      </div>

      {/* Inline delete error */}
      {deleteError && (
        <div className="border-b border-red-200 bg-red-50 px-3 py-1.5 text-[0.75rem] text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          <AlertTriangle className="mr-1 inline h-3 w-3" />
          {deleteError}
        </div>
      )}

      {/* User message */}
      <div className="px-3 py-2.5">
        <div className="mb-1 text-[0.68rem] font-semibold uppercase tracking-wide text-[var(--color-fg-subtle)]">
          User
        </div>
        <div className="rounded-xl bg-[var(--color-user-bubble)] px-3 py-2 text-[0.88rem]">
          <Markdown>{msg.user_message || "_empty_"}</Markdown>
        </div>
      </div>

      {/* Assistant response */}
      <div className="border-t border-[var(--color-border)] px-3 py-2.5">
        <div className="mb-1 text-[0.68rem] font-semibold uppercase tracking-wide text-[var(--color-fg-subtle)]">
          Assistant
        </div>
        <div className="flex gap-2.5">
          <div className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-[var(--color-accent)] text-[0.6rem] text-[var(--color-bg)]">
            🦅
          </div>
          <div className="min-w-0 flex-1 text-[0.88rem]">
            <Markdown>{msg.assistant_response || "_empty_"}</Markdown>
            {canSpeak && !!msg.assistant_response && (
              <div className="mt-1 flex items-center gap-1">
                <SpeakButton id={msg._id} text={msg.assistant_response} />
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Messages view (when a category is selected)
// ---------------------------------------------------------------------------

function CategoryMessagesView({
  identityId,
  category,
  onBack,
}: {
  identityId: string;
  category: Category;
  onBack: () => void;
}) {
  const [page, setPage] = useState(0);
  const qc = useQueryClient();
  const skip = page * PAGE_SIZE;

  // Mirror the canSpeak check from ChatMessage — only show the voice button
  // when a voice_id is configured in the sidebar settings.
  const voiceId = useSettings((s) => s.voice.voice_id);
  const canSpeak = !!voiceId;

  const { data, isLoading, isError, error, refetch } = useCategoryMessages(
    identityId,
    category._id,
    skip,
    PAGE_SIZE,
  );

  const messages = data?.messages ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const hasPrev = page > 0;
  const hasNext = skip + messages.length < total;

  async function handleDeleteMessage(msg: CategoryMessage): Promise<void> {
    await api.deleteCategoryMessage(identityId, category._id, msg._id);
    qc.invalidateQueries({ queryKey: qk.categoryMessages(identityId, category._id) });
    toast.success("Message deleted.");
  }

  return (
    <div className="flex h-full flex-col">
      {/* Sub-header — wraps gracefully on narrow screens */}
      <div className="shrink-0 border-b border-[var(--color-border)] px-3 py-2.5 sm:px-4">
        <div className="flex min-w-0 items-center gap-1.5">
          <button
            onClick={onBack}
            className="flex shrink-0 items-center gap-1 rounded-md px-2 py-1.5 text-[0.8rem] text-[var(--color-fg-muted)] transition-colors hover:bg-[var(--color-surface-2)] hover:text-[var(--color-fg)]"
          >
            <ChevronLeft className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">All categories</span>
            <span className="sm:hidden">Back</span>
          </button>
          <span className="shrink-0 text-[var(--color-fg-subtle)]">/</span>
          <div className="flex min-w-0 flex-1 items-center gap-1.5">
            <Tag className="h-3.5 w-3.5 shrink-0 text-[var(--color-accent)]" />
            <span className="truncate text-[0.88rem] font-semibold text-[var(--color-fg)]">
              {category.name}
            </span>
            {!isError && (
              <span className="shrink-0 text-[0.75rem] text-[var(--color-fg-subtle)]">
                ({total})
              </span>
            )}
          </div>

          {/* PDF download — plain GET link, works on mobile without fetch */}
          {!isError && total > 0 && (
            <a
              href={api.exportCategoryPdf(identityId, category._id)}
              download
              className="ml-auto flex shrink-0 items-center gap-1.5 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-2.5 py-1.5 text-[0.78rem] text-[var(--color-fg-muted)] transition-colors hover:border-[var(--color-accent)] hover:text-[var(--color-fg)]"
              title="Download all messages as PDF"
            >
              <Download className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">PDF</span>
            </a>
          )}
        </div>
      </div>

      {/* Content */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl space-y-3 px-3 py-4 sm:px-4">
          {isLoading ? (
            <div className="flex items-center justify-center gap-2 py-20 text-[var(--color-fg-subtle)]">
              <Spinner /> Loading messages…
            </div>
          ) : isError ? (
            <ErrorBanner
              message={
                (error as Error)?.message ||
                "Failed to load messages. Check the backend connection."
              }
              onRetry={() => refetch()}
            />
          ) : messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-24 text-center">
              <MessageSquare className="mb-3 h-10 w-10 text-[var(--color-fg-subtle)]" />
              <div className="text-[0.88rem] font-medium text-[var(--color-fg)]">
                No messages yet
              </div>
              <div className="mt-1 max-w-xs text-[0.78rem] text-[var(--color-fg-subtle)]">
                Messages categorized as &ldquo;{category.name}&rdquo; will appear here
                automatically after each chat turn completes.
              </div>
            </div>
          ) : (
            <>
              <div className="text-center text-[0.72rem] text-[var(--color-fg-subtle)]">
                Newest first · page {page + 1} of {totalPages}
              </div>
              {messages.map((msg) => (
                <MessagePairCard
                  key={msg._id}
                  msg={msg}
                  onDelete={() => handleDeleteMessage(msg)}
                  canSpeak={canSpeak}
                />
              ))}
            </>
          )}
        </div>
      </div>

      {/* Pagination footer */}
      {!isError && total > PAGE_SIZE && (
        <div className="flex shrink-0 items-center justify-between gap-2 border-t border-[var(--color-border)] px-3 py-2.5 sm:px-4">
          <Button
            size="sm"
            variant="secondary"
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={!hasPrev}
          >
            <ChevronLeft className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">Previous</span>
          </Button>
          <span className="text-[0.78rem] text-[var(--color-fg-muted)]">
            {skip + 1}–{Math.min(skip + messages.length, total)} of {total}
          </span>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => setPage((p) => p + 1)}
            disabled={!hasNext}
          >
            <span className="hidden sm:inline">Next</span>
            <ChevronRight className="h-3.5 w-3.5" />
          </Button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Category list card
// ---------------------------------------------------------------------------

function CategoryCard({
  category,
  onSelect,
  onDelete,
}: {
  category: Category;
  onSelect: () => void;
  onDelete: () => Promise<void>;
}) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function handleDelete(e: React.MouseEvent) {
    e.stopPropagation();
    setDeleting(true);
    setDeleteError(null);
    try {
      await onDelete();
    } catch (err) {
      setDeleteError((err as Error).message);
      setConfirmDelete(false);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] transition-colors hover:border-[var(--color-accent)]">
      <div
        // Only the left part (icon + text) triggers navigation; the right
        // action area is always interactive on both mouse and touch.
        className="flex items-center gap-3 px-3 py-3.5 sm:px-4"
      >
        {/* Icon — tappable to navigate */}
        <button
          onClick={!deleting ? onSelect : undefined}
          className="flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-lg bg-[var(--color-accent)]/10 text-[var(--color-accent)]"
          aria-label={`Open ${category.name}`}
        >
          <FolderOpen className="h-5 w-5" />
        </button>

        {/* Text — tappable to navigate */}
        <button
          onClick={!deleting ? onSelect : undefined}
          className="min-w-0 flex-1 cursor-pointer text-left"
          aria-label={`Open ${category.name}`}
        >
          <div className="truncate text-[0.93rem] font-semibold text-[var(--color-fg)]">
            {category.name}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[0.72rem] text-[var(--color-fg-subtle)]">
            <span>
              {category.message_count ?? 0}{" "}
              {(category.message_count ?? 0) === 1 ? "message" : "messages"}
            </span>
            <span aria-hidden>·</span>
            {/* Full date on ≥sm, short on mobile */}
            <span className="hidden sm:inline">
              Created {formatDateTime(category.created_at)}
            </span>
            <span className="sm:hidden">
              {formatDateTimeShort(category.created_at)}
            </span>
          </div>
        </button>

        {/* Action area — always visible (no opacity-0 on mobile) */}
        <div
          className="flex shrink-0 items-center gap-1"
          onClick={(e) => e.stopPropagation()}
        >
          {confirmDelete ? (
            <ConfirmRow
              label="Delete?"
              onConfirm={handleDelete}
              onCancel={(e) => { e.stopPropagation(); setConfirmDelete(false); }}
              confirming={deleting}
            />
          ) : (
            <>
              <button
                onClick={(e) => { e.stopPropagation(); setConfirmDelete(true); }}
                className="rounded-md p-2 text-[var(--color-fg-subtle)] transition-colors hover:bg-[var(--color-surface-2)] hover:text-[var(--color-red)]"
                title={`Delete "${category.name}"`}
                aria-label={`Delete ${category.name}`}
              >
                <Trash2 className="h-4 w-4" />
              </button>
              {/* Chevron purely decorative — not a button */}
              <ChevronRight
                className="h-4 w-4 text-[var(--color-fg-subtle)]"
                aria-hidden
              />
            </>
          )}
        </div>
      </div>

      {/* Inline delete error */}
      {deleteError && (
        <div className="border-t border-red-200 bg-red-50 px-4 py-1.5 text-[0.75rem] text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          <AlertTriangle className="mr-1 inline h-3 w-3" />
          {deleteError}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main tab
// ---------------------------------------------------------------------------

export function CategoriesTab() {
  const identityId = useSettings((s) => s.identityId);
  const qc = useQueryClient();

  const { data, isLoading, isError, error, refetch } = useCategories(identityId);

  const [selectedCategory, setSelectedCategory] = useState<Category | null>(null);
  const [newCategoryName, setNewCategoryName] = useState("");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);

  const categories = data?.categories ?? [];

  const refreshCategories = () => {
    qc.invalidateQueries({ queryKey: qk.categories(identityId) });
  };

  async function handleAddCategory() {
    const name = newCategoryName.trim();
    if (!name) return;
    setAdding(true);
    setAddError(null);
    try {
      await api.addCategory(identityId, name);
      setNewCategoryName("");
      setShowAdd(false);
      refreshCategories();
      toast.success(`Category "${name}" created.`);
    } catch (err) {
      setAddError((err as Error).message);
    } finally {
      setAdding(false);
    }
  }

  async function handleDeleteCategory(category: Category): Promise<void> {
    await api.deleteCategory(identityId, category._id);
    if (selectedCategory?._id === category._id) setSelectedCategory(null);
    refreshCategories();
    toast.success(`"${category.name}" and all its messages deleted.`);
  }

  if (selectedCategory) {
    return (
      <CategoryMessagesView
        identityId={identityId}
        category={selectedCategory}
        onBack={() => setSelectedCategory(null)}
      />
    );
  }

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="shrink-0 border-b border-[var(--color-border)] px-3 py-3 sm:px-4">
        <div className="mx-auto flex max-w-3xl items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-[1rem] font-semibold text-[var(--color-fg)]">
              Categories
            </h2>
            <p className="mt-0.5 text-[0.76rem] text-[var(--color-fg-subtle)]">
              Auto-classified archive for{" "}
              <span className="font-mono">{identityId}</span>.
            </p>
          </div>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              setShowAdd((v) => !v);
              setAddError(null);
              setNewCategoryName("");
            }}
            className="shrink-0"
          >
            <Plus className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">
              {showAdd ? "Cancel" : "Add category"}
            </span>
            <span className="sm:hidden">{showAdd ? "Cancel" : "Add"}</span>
          </Button>
        </div>

        {/* Add category form */}
        {showAdd && (
          <div className="mx-auto mt-3 max-w-3xl space-y-2">
            <div className="flex gap-2">
              <Input
                value={newCategoryName}
                onChange={(e) => {
                  setNewCategoryName(e.target.value);
                  if (addError) setAddError(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleAddCategory();
                  if (e.key === "Escape") {
                    setShowAdd(false);
                    setNewCategoryName("");
                    setAddError(null);
                  }
                }}
                placeholder="Category name…"
                className="h-9 min-w-0 flex-1 text-[0.88rem]"
                autoFocus
              />
              <Button
                size="sm"
                variant="primary"
                onClick={handleAddCategory}
                loading={adding}
                disabled={!newCategoryName.trim()}
              >
                Create
              </Button>
            </div>
            {addError && (
              <div className="flex items-start gap-1.5 text-[0.75rem] text-red-600 dark:text-red-400">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{addError}</span>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Category list */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl px-3 py-4 sm:px-4">
          {isLoading ? (
            <div className="flex items-center justify-center gap-2 py-20 text-[var(--color-fg-subtle)]">
              <Spinner /> Loading categories…
            </div>
          ) : isError ? (
            <ErrorBanner
              message={
                (error as Error)?.message ||
                "Failed to load categories. Check the backend connection."
              }
              onRetry={() => refetch()}
            />
          ) : categories.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-24 text-center">
              <FolderOpen className="mb-3 h-10 w-10 text-[var(--color-fg-subtle)]" />
              <div className="text-[0.88rem] font-medium text-[var(--color-fg)]">
                No categories yet
              </div>
              <div className="mt-1 max-w-xs text-[0.78rem] text-[var(--color-fg-subtle)]">
                Default categories are seeded automatically when the first message
                is sent for this identity.
              </div>
            </div>
          ) : (
            <div className="space-y-2">
              {categories.map((cat) => (
                <CategoryCard
                  key={cat._id}
                  category={cat}
                  onSelect={() => setSelectedCategory(cat)}
                  onDelete={() => handleDeleteCategory(cat)}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
