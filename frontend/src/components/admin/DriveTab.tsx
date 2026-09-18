"use client";

/**
 * DriveTab — connect the deployment's Google Drive account, once.
 *
 * The whole point of this screen is that it should be visited once and then
 * forgotten. Connecting stores a refresh token server-side; access tokens are
 * minted from it as needed and nothing here expires on a schedule. So the panel
 * is built to answer two questions and nothing else: is it connected, and if
 * not, what exactly is missing.
 *
 * Every piece of configuration that is absent is listed as its own line with the
 * fix in it, because the alternative — one "not configured" message — means
 * setting an environment variable, redeploying, and discovering the next missing
 * one. Three deploys to learn three variable names is not a setup flow.
 */

import { useCallback, useEffect, useState } from "react";
import {
  CheckCircle2,
  ExternalLink,
  HardDrive,
  Link2Off,
  RefreshCw,
  TriangleAlert,
  XCircle,
} from "lucide-react";
import { driveApi, type DriveStatus } from "@/lib/adminApi";
import { cn } from "@/lib/utils";

function fmt(when: string | null): string {
  if (!when) return "—";
  const d = new Date(when);
  return Number.isNaN(d.getTime()) ? when : d.toLocaleString();
}

/** The scope URLs are long and the only part that differs is the last segment. */
function shortScopes(scopes: string): string {
  return (scopes || "")
    .split(/\s+/)
    .filter(Boolean)
    .map((s) => s.replace("https://www.googleapis.com/auth/", ""))
    .join(", ");
}

export function DriveTab() {
  const [status, setStatus] = useState<DriveStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<"connect" | "check" | "disconnect" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);

  const load = useCallback(async () => {
    try {
      setStatus(await driveApi.status());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function connect() {
    setBusy("connect");
    setError(null);
    setNote(null);
    try {
      const { auth_url } = await driveApi.connect();
      // A new tab rather than a redirect: the consent screen ends on a backend
      // page, and navigating there would lose the admin panel and the session
      // state behind it. They approve, close the tab, and press Re-check.
      window.open(auth_url, "_blank", "noopener,noreferrer");
      setNote(
        "Approve access in the tab that just opened, then come back and press Re-check.",
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function check() {
    setBusy("check");
    setError(null);
    setNote(null);
    try {
      const res = await driveApi.check();
      setStatus(res);
      setNote(
        `Working — refreshed the token and read ${
          res.folder_name ? `"${res.folder_name}"` : "the folder"
        }.`,
      );
    } catch (e) {
      setError((e as Error).message);
      load();
    } finally {
      setBusy(null);
    }
  }

  async function disconnect() {
    setBusy("disconnect");
    setError(null);
    setNote(null);
    try {
      setStatus(await driveApi.disconnect());
      setNote("Disconnected, and the credential was revoked at Google.");
      setConfirmDisconnect(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (loading) {
    return <div className="p-6 text-sm text-[var(--color-fg-muted)]">Loading…</div>;
  }

  const connected = !!status?.connected;
  const configured = !!status?.configured;
  const health: "ok" | "warn" | "down" = status?.last_error
    ? "down"
    : connected && !status?.folder_changed_since_connect
      ? "ok"
      : "warn";

  return (
    <div className="max-w-2xl space-y-4 p-6">
      <div>
        <h3 className="flex items-center gap-2 text-sm font-semibold text-[var(--color-fg)]">
          <HardDrive className="h-4 w-4" /> Google Drive
        </h3>
        <p className="mt-1 text-xs leading-relaxed text-[var(--color-fg-muted)]">
          One folder, shared by the whole deployment. The{" "}
          <span className="font-mono">drive_list</span>,{" "}
          <span className="font-mono">drive_summarize</span> and{" "}
          <span className="font-mono">drive_upload</span> agents all work inside it.
          Connect once — the credential is stored and refreshes itself, so this does not
          need doing again.
        </p>
      </div>

      {/* ── State ── */}
      <div className="space-y-2 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3">
        <div className="flex items-center gap-2">
          {health === "ok" ? (
            <CheckCircle2 className="h-4 w-4 shrink-0 text-green-500" />
          ) : health === "down" ? (
            <XCircle className="h-4 w-4 shrink-0 text-red-500" />
          ) : (
            <TriangleAlert className="h-4 w-4 shrink-0 text-amber-500" />
          )}
          <span className="text-sm font-medium">
            {connected ? "Connected" : configured ? "Not connected yet" : "Not configured"}
          </span>
          {connected && status?.account_email && (
            <span className="truncate font-mono text-xs text-[var(--color-fg-muted)]">
              {status.account_email}
            </span>
          )}
        </div>

        {connected && (
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 pt-1 text-xs">
            <dt className="text-[var(--color-fg-subtle)]">Folder</dt>
            <dd className="truncate font-mono text-[var(--color-fg-muted)]">
              {status?.folder_name || status?.folder_id || "—"}
            </dd>
            <dt className="text-[var(--color-fg-subtle)]">Access</dt>
            <dd className="text-[var(--color-fg-muted)]">
              {shortScopes(status?.granted_scopes || status?.scopes || "")}
            </dd>
            <dt className="text-[var(--color-fg-subtle)]">Connected</dt>
            <dd className="text-[var(--color-fg-muted)]">
              {fmt(status?.connected_at ?? null)}
              {status?.connected_by ? ` by ${status.connected_by}` : ""}
            </dd>
            <dt className="text-[var(--color-fg-subtle)]">Last refresh</dt>
            <dd className="text-[var(--color-fg-muted)]">{fmt(status?.last_refresh_at ?? null)}</dd>
            <dt className="text-[var(--color-fg-subtle)]">Summary model</dt>
            <dd className="font-mono text-[var(--color-fg-muted)]">{status?.summary_model}</dd>
          </dl>
        )}
      </div>

      {/* Configuration still missing. Listed in full so one pass through the
          environment variables fixes everything rather than one thing. */}
      {!configured && status?.problems?.length ? (
        <div className="space-y-1.5 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2.5 text-xs text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
          <p className="font-medium">Set these before connecting:</p>
          <ul className="list-inside list-disc space-y-1 leading-relaxed">
            {status.problems.map((p, i) => (
              <li key={i}>{p}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {status?.folder_changed_since_connect && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
          The configured folder has changed since Drive was connected. Press Re-check — if
          it fails, reconnect so the grant covers the new folder.
        </div>
      )}

      {status?.last_error && (
        <div className="rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          {status.last_error}
        </div>
      )}

      {error && (
        <div className="whitespace-pre-wrap rounded-lg border border-red-300 bg-red-50 px-3 py-2 font-mono text-xs text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </div>
      )}

      {note && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-2)] px-3 py-2 text-xs text-[var(--color-fg-muted)]">
          {note}
        </div>
      )}

      {/* ── Actions ── */}
      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={connect}
          disabled={!configured || busy !== null}
          className="flex items-center gap-2 rounded-lg bg-[var(--color-fg)] px-4 py-2.5 text-sm font-semibold text-[var(--color-bg)] transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          <ExternalLink className="h-4 w-4" />
          {busy === "connect"
            ? "Opening…"
            : connected
              ? "Reconnect"
              : "Connect Google Drive"}
        </button>

        {connected && (
          <button
            onClick={check}
            disabled={busy !== null}
            className="flex items-center gap-2 rounded-lg border border-[var(--color-border)] px-3 py-2 text-xs text-[var(--color-fg-muted)] transition-colors hover:bg-[var(--color-surface)] hover:text-[var(--color-fg)] disabled:opacity-50"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", busy === "check" && "spin")} />
            {busy === "check" ? "Checking…" : "Re-check"}
          </button>
        )}

        {connected &&
          (confirmDisconnect ? (
            <span className="flex items-center gap-1 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1">
              <span className="text-xs text-[var(--color-fg-muted)]">
                Disconnect and revoke?
              </span>
              <button
                onClick={disconnect}
                disabled={busy !== null}
                autoFocus
                className="rounded px-2 py-0.5 text-xs font-semibold text-red-600 transition-colors hover:bg-red-50 disabled:opacity-50 dark:hover:bg-red-950/40"
              >
                Yes
              </button>
              <button
                onClick={() => setConfirmDisconnect(false)}
                className="rounded px-2 py-0.5 text-xs text-[var(--color-fg-muted)] transition-colors hover:bg-[var(--color-bg)]"
              >
                No
              </button>
            </span>
          ) : (
            <button
              onClick={() => setConfirmDisconnect(true)}
              disabled={busy !== null}
              className="ml-auto flex items-center gap-2 rounded-lg border border-[var(--color-border)] px-3 py-2 text-xs text-[var(--color-fg-muted)] transition-colors hover:border-red-300 hover:text-red-600 disabled:opacity-50"
            >
              <Link2Off className="h-3.5 w-3.5" /> Disconnect
            </button>
          ))}
      </div>

      {configured && !connected && (
        <p className="text-xs leading-relaxed text-[var(--color-fg-subtle)]">
          Sign in as the account that owns the folder. If Google returns you here without
          a working connection, the usual cause is that the OAuth client is still in
          Testing mode in the Cloud Console — publish it, or its credential will expire
          every seven days.
        </p>
      )}
    </div>
  );
}
