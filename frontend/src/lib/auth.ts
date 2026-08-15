/**
 * auth.ts — Auth API helpers and token management.
 *
 * Token is stored in localStorage under 'falcon-auth-token'.
 * The key 'falcon-auth-user' stores the decoded user info (username, role).
 *
 * All API calls in api.ts that need auth should use getAuthHeaders().
 */

import { API_BASE } from "./api";

const TOKEN_KEY = "falcon-auth-token";
const USER_KEY = "falcon-auth-user";

export interface AuthUser {
  username: string;
  role: string;
  identity_id: string;
  features: Record<string, boolean> | null; // null = admin (full access)
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function getAuthUser(): AuthUser | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as AuthUser) : null;
  } catch {
    return null;
  }
}

export function setAuth(token: string, user: AuthUser): void {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function clearAuth(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

export function getAuthHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function isAuthenticated(): boolean {
  return !!getToken();
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------

export interface LoginResult {
  access_token: string;
  token_type: string;
  username: string;
  role: string;
  identity_id: string;
  features: Record<string, boolean> | null;
}

export async function loginRequest(
  username: string,
  password: string
): Promise<LoginResult> {
  const res = await fetch(`${API_BASE}/api/admin/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<LoginResult>;
}

// Verify the current token is still valid against /admin/me
export async function verifyToken(): Promise<AuthUser | null> {
  const token = getToken();
  if (!token) return null;
  try {
    const res = await fetch(`${API_BASE}/api/admin/me`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) return null;
    const data = await res.json();
    // Re-read features from stored user (me endpoint doesn't return them)
    const stored = getAuthUser();
    return {
      username: data.username,
      role: data.role,
      identity_id: data.identity_id ?? "default",
      features: stored?.features ?? null,
    };
  } catch {
    return null;
  }
}
