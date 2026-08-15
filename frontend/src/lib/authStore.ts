/**
 * authStore.ts — Zustand store for authentication state.
 *
 * isLoading is ONLY true during the one-time startup token verification.
 * It is never set true during login — that would replace the login form with
 * a full-screen spinner and hide error messages when login fails.
 */

import { create } from "zustand";
import type { AuthUser } from "./auth";
import { clearAuth, isAuthenticated, setAuth, verifyToken, loginRequest } from "./auth";

interface AuthState {
  /** True only during the initial JWT verification on app mount. */
  isLoading: boolean;
  isAuthenticated: boolean;
  user: AuthUser | null;
  error: string | null;

  /** Call once on app mount. Idempotent — subsequent calls are no-ops. */
  initialize: () => Promise<void>;
  /** Log in with username + password. Throws on failure so LoginPage can react. */
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
  clearError: () => void;
}

// Tracks whether initialize has already run so re-renders of AuthGuard
// don't re-trigger it. A module-level flag is safer than putting it in the
// store because store state changes cause useEffect deps to fire again.
let _initialized = false;

function applyIdentityToStore(identity_id: string) {
  import("./store").then(({ useSettings }) => {
    useSettings.getState().setIdentity(identity_id);
  });
}

export const useAuth = create<AuthState>()((set) => ({
  isLoading: true,   // true on first render; AuthGuard shows spinner until initialize() finishes
  isAuthenticated: false,
  user: null,
  error: null,

  initialize: async () => {
    // Run at most once per page load
    if (_initialized) return;
    _initialized = true;

    if (!isAuthenticated()) {
      set({ isLoading: false, isAuthenticated: false, user: null });
      return;
    }

    const user = await verifyToken();
    if (user) {
      applyIdentityToStore(user.identity_id);
      set({ isLoading: false, isAuthenticated: true, user });
    } else {
      clearAuth();
      set({ isLoading: false, isAuthenticated: false, user: null });
    }
  },

  login: async (username, password) => {
    // Do NOT touch isLoading here — AuthGuard must keep showing LoginPage
    // so the user can see the error if login fails.
    set({ error: null });
    try {
      const result = await loginRequest(username, password);
      const user: AuthUser = {
        username: result.username,
        role: result.role,
        identity_id: result.identity_id,
        features: result.features ?? null,
      };
      setAuth(result.access_token, user);
      applyIdentityToStore(result.identity_id);
      set({ isAuthenticated: true, user, error: null });
    } catch (e) {
      // Set error — LoginPage reads this and shows the red banner
      set({ error: (e as Error).message || "Incorrect username or password." });
      throw e;   // re-throw so LoginPage can trigger the shake animation
    }
  },

  logout: () => {
    // Reset the init flag so initialize() runs again after next login if needed
    _initialized = false;
    clearAuth();
    set({ isAuthenticated: false, user: null, error: null });
  },

  clearError: () => set({ error: null }),
}));
