/**
 * adminApi.ts — Typed API client for the admin panel endpoints.
 */

import { API_BASE } from "./api";
import { getAuthHeaders, clearAuth } from "./auth";

export const ALL_FEATURES = [
  "chat",
  "memory",
  "context",
  "categories",
  "audit",
  "logs",
  "testing",
  "dualrun",
  "polymarket",
  "kalshi",
  "voice",
] as const;

export type FeatureKey = (typeof ALL_FEATURES)[number];

export interface PortalUser {
  id: string;
  username: string;
  display_name: string;
  disabled: boolean;
  features: Record<FeatureKey, boolean>;
  created_at: string;
  updated_at: string;
}

export interface AdminAuditRecord {
  timestamp: string;
  actor: string;
  action: string;
  target: string | null;
  details: Record<string, unknown>;
}

async function adminReq<T>(
  path: string,
  init?: RequestInit
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...getAuthHeaders(),
      ...(init?.headers ?? {}),
    },
    signal: init?.signal ?? AbortSignal.timeout(20_000),
  });

  if (res.status === 401) {
    // Token expired — clear auth and force reload to show login
    clearAuth();
    window.location.reload();
    throw new Error("Session expired. Please log in again.");
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }

  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const adminApi = {
  // Users
  listUsers: () =>
    adminReq<{ users: PortalUser[] }>("/api/admin/users").then((r) => r.users),

  getUser: (id: string) =>
    adminReq<PortalUser>(`/api/admin/users/${encodeURIComponent(id)}`),

  createUser: (body: {
    username: string;
    password: string;
    display_name?: string;
    features?: Record<string, boolean>;
  }) =>
    adminReq<PortalUser>("/api/admin/users", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updateUser: (
    id: string,
    patch: { display_name?: string; password?: string; disabled?: boolean }
  ) =>
    adminReq<PortalUser>(`/api/admin/users/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  deleteUser: (id: string) =>
    adminReq<{ deleted: string }>(`/api/admin/users/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),

  setPermissions: (id: string, features: Record<string, boolean>) =>
    adminReq<PortalUser>(
      `/api/admin/users/${encodeURIComponent(id)}/permissions`,
      { method: "PUT", body: JSON.stringify({ features }) }
    ),

  // Audit log
  getAuditLog: (limit = 200) =>
    adminReq<{ records: AdminAuditRecord[] }>(
      `/api/admin/audit?limit=${limit}`
    ).then((r) => r.records),
};
