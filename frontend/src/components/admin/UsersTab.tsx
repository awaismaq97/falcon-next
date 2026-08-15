"use client";

import { useState, useEffect, useCallback } from "react";
import {
  Plus,
  Pencil,
  Trash2,
  Eye,
  EyeOff,
  UserCheck,
  UserX,
  RefreshCw,
} from "lucide-react";
import { adminApi, ALL_FEATURES, type PortalUser } from "@/lib/adminApi";
import { cn } from "@/lib/utils";

const FEATURE_LABELS: Record<string, string> = {
  chat: "Chat",
  memory: "Memory",
  context: "Context",
  categories: "Categories",
  audit: "Audit",
  logs: "Logs",
  testing: "Testing",
  dualrun: "Dual Run",
  polymarket: "Poly Market",
  kalshi: "Kalshi",
  voice: "Voice",
};

// ─────────────────────────────────────────────────────────────────────────────
// Main tab
// ─────────────────────────────────────────────────────────────────────────────
export function UsersTab() {
  const [users, setUsers] = useState<PortalUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [editUser, setEditUser] = useState<PortalUser | null>(null);
  const [permUser, setPermUser] = useState<PortalUser | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setUsers(await adminApi.listUsers());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function handleDelete(id: string) {
    try {
      await adminApi.deleteUser(id);
      setDeleteConfirm(null);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function handleToggleDisable(user: PortalUser) {
    try {
      await adminApi.updateUser(user.id, { disabled: !user.disabled });
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function openCreate() { setShowCreate(true); setEditUser(null); setPermUser(null); }
  function openEdit(u: PortalUser) { setEditUser(u); setShowCreate(false); setPermUser(null); }
  function openPerms(u: PortalUser) { setPermUser(u); setShowCreate(false); setEditUser(null); }

  return (
    <div className="p-4 sm:p-6">
      {/* ── Toolbar ── */}
      <div className="mb-4 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-[var(--color-fg)]">
          Portal Users
          <span className="ml-2 text-xs font-normal text-[var(--color-fg-subtle)]">
            ({users.length})
          </span>
        </h3>
        <div className="flex items-center gap-2">
          <button
            onClick={load}
            className="rounded-lg p-2 text-[var(--color-fg-muted)] hover:bg-[var(--color-surface)] hover:text-[var(--color-fg)] transition-colors"
            title="Refresh"
          >
            <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
          </button>
          <button
            onClick={openCreate}
            className="flex items-center gap-1.5 rounded-lg bg-[var(--color-fg)] px-3 py-2 text-xs font-semibold text-[var(--color-bg)] hover:opacity-90 transition-opacity"
          >
            <Plus className="h-3.5 w-3.5" />
            <span>New User</span>
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </div>
      )}

      {/* ── Inline forms ── */}
      {showCreate && (
        <CreateUserForm
          onCreated={async () => { setShowCreate(false); await load(); }}
          onCancel={() => setShowCreate(false)}
        />
      )}
      {editUser && (
        <EditUserForm
          user={editUser}
          onSaved={async () => { setEditUser(null); await load(); }}
          onCancel={() => setEditUser(null)}
        />
      )}
      {permUser && (
        <PermissionsForm
          user={permUser}
          onSaved={async (updated) => {
            setPermUser(null);
            setUsers((prev) => prev.map((u) => (u.id === updated.id ? updated : u)));
          }}
          onCancel={() => setPermUser(null)}
        />
      )}

      {/* ── User list ── */}
      {loading && !users.length ? (
        <div className="py-12 text-center text-sm text-[var(--color-fg-subtle)]">Loading users…</div>
      ) : users.length === 0 ? (
        <div className="rounded-xl border border-dashed border-[var(--color-border)] py-12 text-center text-sm text-[var(--color-fg-subtle)]">
          No users yet. Create the first one above.
        </div>
      ) : (
        /* Card list on mobile, table on sm+ */
        <div className="space-y-3 sm:space-y-0">
          {/* Mobile card view */}
          <div className="flex flex-col gap-3 sm:hidden">
            {users.map((user) => (
              <UserCard
                key={user.id}
                user={user}
                deleteConfirm={deleteConfirm}
                onEdit={() => openEdit(user)}
                onPerms={() => openPerms(user)}
                onToggleDisable={() => handleToggleDisable(user)}
                onDeleteRequest={() => setDeleteConfirm(user.id)}
                onDeleteConfirm={() => handleDelete(user.id)}
                onDeleteCancel={() => setDeleteConfirm(null)}
              />
            ))}
          </div>

          {/* Desktop table view */}
          <div className="hidden sm:block overflow-x-auto rounded-xl border border-[var(--color-border)]">
            <table className="w-full text-sm">
              <thead className="bg-[var(--color-surface)]">
                <tr>
                  {["Username", "Display Name", "Status", "Features", "Created", ""].map((h) => (
                    <th key={h} className="px-4 py-3 text-left text-xs font-medium text-[var(--color-fg-muted)]">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--color-border)]">
                {users.map((user) => (
                  <tr
                    key={user.id}
                    className={cn("transition-colors hover:bg-[var(--color-surface)]/50", user.disabled && "opacity-60")}
                  >
                    <td className="px-4 py-3 font-mono text-[0.82rem]">{user.username}</td>
                    <td className="px-4 py-3 text-[0.82rem] text-[var(--color-fg-muted)]">
                      {user.display_name || <span className="italic text-[var(--color-fg-subtle)]">—</span>}
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge disabled={user.disabled} />
                    </td>
                    <td className="px-4 py-3">
                      <FeaturePills features={user.features} />
                    </td>
                    <td className="px-4 py-3 text-[0.75rem] text-[var(--color-fg-subtle)] whitespace-nowrap">
                      {user.created_at ? new Date(user.created_at).toLocaleDateString() : "—"}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-end gap-1">
                        <ActionBtn onClick={() => openEdit(user)} title="Edit"><Pencil className="h-3.5 w-3.5" /></ActionBtn>
                        <ActionBtn onClick={() => openPerms(user)} title="Permissions"><Eye className="h-3.5 w-3.5" /></ActionBtn>
                        <ActionBtn onClick={() => handleToggleDisable(user)} title={user.disabled ? "Enable" : "Disable"}>
                          {user.disabled
                            ? <UserCheck className="h-3.5 w-3.5 text-green-600" />
                            : <UserX className="h-3.5 w-3.5 text-amber-500" />}
                        </ActionBtn>
                        {deleteConfirm === user.id ? (
                          <div className="flex items-center gap-1">
                            <button onClick={() => handleDelete(user.id)} className="rounded px-2 py-1 text-xs font-semibold text-red-600 hover:bg-red-50 dark:hover:bg-red-950/40">Confirm</button>
                            <button onClick={() => setDeleteConfirm(null)} className="rounded px-2 py-1 text-xs text-[var(--color-fg-muted)] hover:bg-[var(--color-surface)]">Cancel</button>
                          </div>
                        ) : (
                          <ActionBtn onClick={() => setDeleteConfirm(user.id)} title="Delete" danger>
                            <Trash2 className="h-3.5 w-3.5" />
                          </ActionBtn>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Mobile user card
// ─────────────────────────────────────────────────────────────────────────────
function UserCard({
  user,
  deleteConfirm,
  onEdit,
  onPerms,
  onToggleDisable,
  onDeleteRequest,
  onDeleteConfirm,
  onDeleteCancel,
}: {
  user: PortalUser;
  deleteConfirm: string | null;
  onEdit: () => void;
  onPerms: () => void;
  onToggleDisable: () => void;
  onDeleteRequest: () => void;
  onDeleteConfirm: () => void;
  onDeleteCancel: () => void;
}) {
  return (
    <div className={cn(
      "rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4",
      user.disabled && "opacity-60"
    )}>
      {/* Top row: username + status */}
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="font-mono text-sm font-semibold text-[var(--color-fg)] truncate">{user.username}</span>
        <StatusBadge disabled={user.disabled} />
      </div>

      {/* Display name */}
      {user.display_name && (
        <p className="mb-2 text-xs text-[var(--color-fg-muted)]">{user.display_name}</p>
      )}

      {/* Features */}
      <div className="mb-3">
        <FeaturePillsCompact features={user.features} />
      </div>

      {/* Created */}
      <p className="mb-3 text-[0.7rem] text-[var(--color-fg-subtle)]">
        Created {user.created_at ? new Date(user.created_at).toLocaleDateString() : "—"}
      </p>

      {/* Actions */}
      {deleteConfirm === user.id ? (
        <div className="flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 dark:border-red-900 dark:bg-red-950/40 px-3 py-2">
          <span className="flex-1 text-xs text-red-700 dark:text-red-300">Delete this user?</span>
          <button onClick={onDeleteConfirm} className="rounded px-3 py-1 text-xs font-semibold text-red-600 bg-red-100 hover:bg-red-200 dark:bg-red-900/60 dark:hover:bg-red-900 transition-colors">
            Delete
          </button>
          <button onClick={onDeleteCancel} className="rounded px-3 py-1 text-xs text-[var(--color-fg-muted)] hover:bg-[var(--color-bg)] transition-colors">
            Cancel
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-4 gap-2">
          <MobileActionBtn onClick={onEdit} label="Edit" icon={<Pencil className="h-4 w-4" />} />
          <MobileActionBtn onClick={onPerms} label="Perms" icon={<Eye className="h-4 w-4" />} />
          <MobileActionBtn
            onClick={onToggleDisable}
            label={user.disabled ? "Enable" : "Disable"}
            icon={user.disabled
              ? <UserCheck className="h-4 w-4 text-green-600" />
              : <UserX className="h-4 w-4 text-amber-500" />}
          />
          <MobileActionBtn onClick={onDeleteRequest} label="Delete" icon={<Trash2 className="h-4 w-4 text-red-500" />} danger />
        </div>
      )}
    </div>
  );
}

function MobileActionBtn({ onClick, label, icon, danger }: { onClick: () => void; label: string; icon: React.ReactNode; danger?: boolean }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex flex-col items-center gap-1 rounded-lg border px-2 py-2 text-[0.65rem] font-medium transition-colors",
        danger
          ? "border-red-200 text-red-500 hover:bg-red-50 dark:border-red-900 dark:hover:bg-red-950/40"
          : "border-[var(--color-border)] text-[var(--color-fg-muted)] hover:bg-[var(--color-bg)] hover:text-[var(--color-fg)]"
      )}
    >
      {icon}
      {label}
    </button>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Create user form
// ─────────────────────────────────────────────────────────────────────────────
function CreateUserForm({ onCreated, onCancel }: { onCreated: () => void; onCancel: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [features, setFeatures] = useState<Record<string, boolean>>(
    Object.fromEntries(ALL_FEATURES.map((f) => [f, true]))
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!username.trim() || !password) return;
    setSubmitting(true);
    setError(null);
    try {
      await adminApi.createUser({ username: username.trim(), password, display_name: displayName.trim(), features });
      onCreated();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mb-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4 sm:p-5">
      <h4 className="mb-4 text-sm font-semibold text-[var(--color-fg)]">Create New User</h4>
      <form onSubmit={handleSubmit} noValidate className="space-y-4">
        {/* Username + Display Name stacked on mobile, side by side on sm+ */}
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Username *">
            <input type="text" autoComplete="off" required value={username}
              onChange={(e) => setUsername(e.target.value)} className={fieldCls} placeholder="e.g. john_doe" />
          </Field>
          <Field label="Display Name">
            <input type="text" value={displayName}
              onChange={(e) => setDisplayName(e.target.value)} className={fieldCls} placeholder="e.g. John Doe" />
          </Field>
        </div>
        <Field label="Password *">
          <PwInput value={password} onChange={setPassword} show={showPw} onToggle={() => setShowPw(v => !v)} placeholder="Set a strong password" />
        </Field>
        <div>
          <p className="mb-2 text-xs font-medium text-[var(--color-fg-muted)]">Features</p>
          <FeatureCheckboxes features={features} onChange={setFeatures} />
        </div>
        {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
        <FormActions submitting={submitting} disabled={!username.trim() || !password} submitLabel="Create User" onCancel={onCancel} />
      </form>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Edit user form
// ─────────────────────────────────────────────────────────────────────────────
function EditUserForm({ user, onSaved, onCancel }: { user: PortalUser; onSaved: () => void; onCancel: () => void }) {
  const [displayName, setDisplayName] = useState(user.display_name ?? "");
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const patch: Record<string, unknown> = { display_name: displayName.trim() };
    if (password) patch.password = password;
    setSubmitting(true);
    setError(null);
    try {
      await adminApi.updateUser(user.id, patch);
      onSaved();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mb-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4 sm:p-5">
      <h4 className="mb-4 text-sm font-semibold text-[var(--color-fg)]">
        Edit: <span className="font-mono text-[var(--color-fg-muted)]">{user.username}</span>
      </h4>
      <form onSubmit={handleSubmit} noValidate className="space-y-4">
        <Field label="Display Name">
          <input type="text" value={displayName} onChange={(e) => setDisplayName(e.target.value)} className={fieldCls} />
        </Field>
        <Field label="New Password (blank = keep current)">
          <PwInput value={password} onChange={setPassword} show={showPw} onToggle={() => setShowPw(v => !v)} placeholder="Leave blank to keep unchanged" />
        </Field>
        {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
        <FormActions submitting={submitting} submitLabel="Save Changes" onCancel={onCancel} />
      </form>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Permissions form
// ─────────────────────────────────────────────────────────────────────────────
function PermissionsForm({ user, onSaved, onCancel }: { user: PortalUser; onSaved: (u: PortalUser) => void; onCancel: () => void }) {
  const [features, setFeatures] = useState<Record<string, boolean>>({
    ...Object.fromEntries(ALL_FEATURES.map((f) => [f, false])),
    ...user.features,
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setSubmitting(true);
    setError(null);
    try {
      const updated = await adminApi.setPermissions(user.id, features);
      onSaved(updated);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mb-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4 sm:p-5">
      <h4 className="mb-4 text-sm font-semibold text-[var(--color-fg)]">
        Permissions: <span className="font-mono text-[var(--color-fg-muted)]">{user.username}</span>
      </h4>
      <FeatureCheckboxes features={features} onChange={setFeatures} />
      {error && <p className="mt-3 text-xs text-red-600 dark:text-red-400">{error}</p>}
      <div className="mt-4">
        <FormActions submitting={submitting} submitLabel="Save Permissions" onCancel={onCancel} onSubmit={handleSave} />
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Shared primitives
// ─────────────────────────────────────────────────────────────────────────────

function StatusBadge({ disabled }: { disabled: boolean }) {
  return (
    <span className={cn(
      "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap",
      disabled
        ? "bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300"
        : "bg-green-100 text-green-700 dark:bg-green-950/40 dark:text-green-300"
    )}>
      {disabled ? "Disabled" : "Active"}
    </span>
  );
}

/** Desktop table feature pills — show first 3, expand on click */
function FeaturePills({ features }: { features: Record<string, boolean> }) {
  const [expanded, setExpanded] = useState(false);
  const enabled = ALL_FEATURES.filter((f) => features[f]);
  const shown = expanded ? enabled : enabled.slice(0, 3);
  const hiddenCount = enabled.length - 3;

  return (
    <div className="flex flex-wrap items-center gap-1">
      {shown.map((f) => (
        <span key={f} className="rounded-full bg-green-100 px-2 py-0.5 text-[0.65rem] font-medium text-green-700 dark:bg-green-950/40 dark:text-green-300">
          {FEATURE_LABELS[f] ?? f}
        </span>
      ))}
      {!expanded && hiddenCount > 0 && (
        <button onClick={() => setExpanded(true)} className="text-[0.65rem] text-[var(--color-fg-subtle)] hover:text-[var(--color-fg-muted)] underline">
          +{hiddenCount}
        </button>
      )}
      {expanded && (
        <button onClick={() => setExpanded(false)} className="text-[0.65rem] text-[var(--color-fg-subtle)] hover:text-[var(--color-fg-muted)] underline">
          less
        </button>
      )}
    </div>
  );
}

/** Mobile card feature summary — single line */
function FeaturePillsCompact({ features }: { features: Record<string, boolean> }) {
  const enabled = ALL_FEATURES.filter((f) => features[f]);
  const disabled = ALL_FEATURES.filter((f) => !features[f]);
  return (
    <div className="flex flex-wrap gap-1">
      {enabled.map((f) => (
        <span key={f} className="rounded-full bg-green-100 px-2 py-0.5 text-[0.65rem] font-medium text-green-700 dark:bg-green-950/40 dark:text-green-300">
          {FEATURE_LABELS[f] ?? f}
        </span>
      ))}
      {disabled.map((f) => (
        <span key={f} className="rounded-full border border-[var(--color-border)] px-2 py-0.5 text-[0.65rem] text-[var(--color-fg-subtle)] line-through">
          {FEATURE_LABELS[f] ?? f}
        </span>
      ))}
    </div>
  );
}

function FeatureCheckboxes({ features, onChange }: { features: Record<string, boolean>; onChange: (f: Record<string, boolean>) => void }) {
  return (
    <div>
      <div className="mb-2 flex gap-3">
        <button type="button" onClick={() => onChange(Object.fromEntries(ALL_FEATURES.map(f => [f, true])))}
          className="text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg)] underline">
          Enable all
        </button>
        <button type="button" onClick={() => onChange(Object.fromEntries(ALL_FEATURES.map(f => [f, false])))}
          className="text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg)] underline">
          Disable all
        </button>
      </div>
      {/* 2 cols on mobile, 3 on sm+ */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {ALL_FEATURES.map((f) => (
          <label key={f} className="flex cursor-pointer items-center gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-xs hover:bg-[var(--color-surface)] transition-colors">
            <input type="checkbox" checked={!!features[f]}
              onChange={(e) => onChange({ ...features, [f]: e.target.checked })}
              className="h-3.5 w-3.5 accent-[var(--color-fg)]" />
            <span className={cn(features[f] ? "text-[var(--color-fg)]" : "text-[var(--color-fg-subtle)] line-through")}>
              {FEATURE_LABELS[f] ?? f}
            </span>
          </label>
        ))}
      </div>
    </div>
  );
}

function PwInput({ value, onChange, show, onToggle, placeholder }: {
  value: string; onChange: (v: string) => void; show: boolean; onToggle: () => void; placeholder?: string;
}) {
  return (
    <div className="relative">
      <input
        type={show ? "text" : "password"}
        autoComplete="new-password"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={cn(fieldCls, "pr-10")}
        placeholder={placeholder}
      />
      <button type="button" tabIndex={-1} onClick={onToggle}
        className="absolute right-3 top-1/2 -translate-y-1/2 text-[var(--color-fg-subtle)] hover:text-[var(--color-fg-muted)]">
        {show ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
      </button>
    </div>
  );
}

function FormActions({ submitting, disabled, submitLabel, onCancel, onSubmit }: {
  submitting: boolean; disabled?: boolean; submitLabel: string; onCancel: () => void; onSubmit?: () => void;
}) {
  return (
    <div className="flex gap-2">
      <button
        type={onSubmit ? "button" : "submit"}
        onClick={onSubmit}
        disabled={submitting || disabled}
        className="flex items-center gap-1.5 rounded-lg bg-[var(--color-fg)] px-4 py-2 text-xs font-semibold text-[var(--color-bg)] hover:opacity-90 disabled:opacity-40 transition-opacity"
      >
        {submitting && <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-[var(--color-bg)] border-t-transparent" />}
        {submitLabel}
      </button>
      <button type="button" onClick={onCancel}
        className="rounded-lg px-4 py-2 text-xs text-[var(--color-fg-muted)] hover:bg-[var(--color-bg)] transition-colors border border-[var(--color-border)]">
        Cancel
      </button>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="mb-1.5 block text-xs font-medium text-[var(--color-fg-muted)]">{label}</label>
      {children}
    </div>
  );
}

function ActionBtn({ onClick, title, children, danger }: {
  onClick: () => void; title: string; children: React.ReactNode; danger?: boolean;
}) {
  return (
    <button onClick={onClick} title={title}
      className={cn("rounded-lg p-1.5 transition-colors",
        danger ? "text-red-500 hover:bg-red-50 dark:hover:bg-red-950/40"
               : "text-[var(--color-fg-muted)] hover:bg-[var(--color-surface)] hover:text-[var(--color-fg)]")}>
      {children}
    </button>
  );
}

const fieldCls = "w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2.5 text-sm text-[var(--color-fg)] placeholder:text-[var(--color-fg-subtle)] outline-none focus:border-[var(--color-fg-muted)] focus:ring-1 focus:ring-[var(--color-fg-muted)] transition-colors";
