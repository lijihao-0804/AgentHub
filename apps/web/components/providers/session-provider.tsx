"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { ApiError, setTokenRefresher, toApiError } from "@/lib/api/client";
import { login as loginRequest, logout as logoutRequest, refresh as refreshRequest, register as registerRequest } from "@/lib/api/auth";
import { getWorkspace, listOrganizations, listWorkspaces, type Organization, type Workspace } from "@/lib/api/tenancy";

/**
 * Formal control-plane session.
 *
 * Invariants:
 *  - The access token lives in React state only. It is never written to
 *    localStorage, sessionStorage, IndexedDB, the URL, or a JS cookie.
 *  - The refresh token is owned by the backend as an HttpOnly cookie and
 *    is never read here; it only travels via `credentials: "include"`.
 *  - `sessionId` is the single frontend session generation. It increments
 *    on sign-in, sign-out and every workspace switch, so workspace-scoped
 *    pages can drop stale responses and reset their own state.
 */

export type SessionStatus = "BOOTSTRAPPING" | "AUTHENTICATED" | "UNAUTHENTICATED";

/** Selected workspace is not a credential, so it may be remembered. */
const WORKSPACE_STORAGE_KEY = "agenthub.workspaceId";
/** Refresh this far before the access token expires (fraction of lifetime). */
const REFRESH_LEAD_RATIO = 0.75;
const MIN_REFRESH_DELAY_MS = 30_000;
/** Refresh this eagerly when the tab wakes up and the token is nearly out. */
const EXPIRY_WAKE_MARGIN_MS = 30_000;

type FrontendSessionContextValue = {
  status: SessionStatus;
  userId: string;
  accessToken: string;
  organizations: Organization[];
  workspaces: Workspace[];
  organizationId: string;
  workspaceId: string;
  /**
   * The signed-in user's permissions in the active workspace, or null while
   * unknown (not yet loaded, or the load failed). Null disables nothing: the
   * backend still enforces every permission, and a transient failure must
   * not brick the UI -- it only loses the role-aware polish.
   */
  permissions: string[] | null;
  /** Monotonic identity for the in-memory session + workspace selection. */
  sessionId: number;
  authenticated: boolean;
  /** Authenticated *and* a workspace is selected — required by scoped pages. */
  connected: boolean;
  tenancyLoading: boolean;
  tenancyError: ApiError | null;
  signIn: (input: { email: string; password: string }) => Promise<void>;
  signUp: (input: { email: string; password: string }) => Promise<void>;
  signOut: () => Promise<void>;
  setActiveWorkspace: (workspaceId: string) => void;
  reloadTenancy: () => Promise<void>;
  panelOpen: boolean;
  openPanel: () => void;
  closePanel: () => void;
};

const FrontendSessionContext = createContext<FrontendSessionContextValue | null>(null);

function readStoredWorkspaceId(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(WORKSPACE_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function storeWorkspaceId(value: string) {
  if (typeof window === "undefined") return;
  try {
    if (value) window.localStorage.setItem(WORKSPACE_STORAGE_KEY, value);
    else window.localStorage.removeItem(WORKSPACE_STORAGE_KEY);
  } catch {
    /* storage unavailable — selection simply does not survive reloads */
  }
}

export function FrontendSessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SessionStatus>("BOOTSTRAPPING");
  const [userId, setUserId] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceId, setWorkspaceId] = useState("");
  const [sessionId, setSessionId] = useState(0);
  const [permissions, setPermissions] = useState<string[] | null>(null);
  const [tenancyLoading, setTenancyLoading] = useState(false);
  const [tenancyError, setTenancyError] = useState<ApiError | null>(null);
  const [panelOpen, setPanelOpen] = useState(false);

  /** Generation for tenancy loads, so a stale list never lands. */
  const tenancyGenerationRef = useRef(0);
  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /**
   * Single-flight refresh shared by the proactive timer and the transport's
   * 401 recovery: one promise per moment, so two triggers can never fire two
   * competing refresh calls (the cookie rotates; the loser would 401 and
   * look like a dead session). Holds the in-flight promise, or null.
   */
  const refreshInFlightRef = useRef<Promise<string | null> | null>(null);
  /** Resolved by `renewAccessToken` below; lets the timer reach it without a
   * declaration-order cycle. */
  const renewRef = useRef<() => Promise<string | null>>(async () => null);
  /** Wall-clock time the current access token expires, for visibility wake-ups. */
  const tokenExpiresAtRef = useRef<number | null>(null);
  /** Generation of active-workspace access loads. */
  const accessGenerationRef = useRef(0);

  const clearRefreshTimer = useCallback(() => {
    if (refreshTimerRef.current !== null) {
      clearTimeout(refreshTimerRef.current);
      refreshTimerRef.current = null;
    }
  }, []);

  const resetToUnauthenticated = useCallback(() => {
    clearRefreshTimer();
    tokenExpiresAtRef.current = null;
    tenancyGenerationRef.current += 1;
    setSessionId((current) => current + 1);
    setStatus("UNAUTHENTICATED");
    setUserId("");
    setAccessToken("");
    setOrganizations([]);
    setWorkspaces([]);
    setWorkspaceId("");
    setPermissions(null);
    setTenancyError(null);
    setTenancyLoading(false);
    setPanelOpen(false);
  }, [clearRefreshTimer]);

  /**
   * Loads the tenancy projection for a token. Returns the workspaces so
   * the caller can pick an active one in the same pass, or ``null`` when
   * the load failed -- a failure must never read as "no workspaces exist".
   */
  const loadTenancy = useCallback(async (token: string): Promise<Workspace[] | null> => {
    const generation = (tenancyGenerationRef.current += 1);
    setTenancyLoading(true);
    setTenancyError(null);
    try {
      const [nextOrganizations, nextWorkspaces] = await Promise.all([
        listOrganizations(token),
        listWorkspaces(token),
      ]);
      if (tenancyGenerationRef.current !== generation) return null;
      if (nextOrganizations === null || nextWorkspaces === null) {
        // Envelope drift is a failed read, not an empty directory.
        throw new ApiError(
          "TENANCY_ENVELOPE_INVALID",
          "The tenancy response had an unexpected shape.",
          0,
        );
      }
      setOrganizations(nextOrganizations);
      setWorkspaces(nextWorkspaces);
      return nextWorkspaces;
    } catch (caught) {
      if (tenancyGenerationRef.current === generation) {
        setOrganizations([]);
        setWorkspaces([]);
        setTenancyError(toApiError(caught, ""));
      }
      return null;
    } finally {
      if (tenancyGenerationRef.current === generation) setTenancyLoading(false);
    }
  }, []);

  const selectInitialWorkspace = useCallback((available: Workspace[]) => {
    const remembered = readStoredWorkspaceId();
    const match = available.find((workspace) => workspace.id === remembered);
    const next = match?.id ?? available[0]?.id ?? "";
    storeWorkspaceId(next);
    setWorkspaceId(next);
  }, []);

  /** Schedules a proactive refresh; never retries in a loop on failure. */
  const scheduleRefresh = useCallback(
    (expiresInSeconds: number | null | undefined) => {
      clearRefreshTimer();
      if (!expiresInSeconds || expiresInSeconds <= 0) {
        // No usable lifetime from the backend: drop any stale expiry so the
        // visibility wake-up cannot act on a token that is already gone.
        tokenExpiresAtRef.current = null;
        return;
      }
      tokenExpiresAtRef.current = Date.now() + expiresInSeconds * 1000;
      const delay = Math.max(MIN_REFRESH_DELAY_MS, expiresInSeconds * REFRESH_LEAD_RATIO * 1000);
      refreshTimerRef.current = setTimeout(() => {
        void renewRef.current();
      }, delay);
    },
    [clearRefreshTimer],
  );

  /**
   * Refreshes the access token once no matter how many callers ask at the
   * same moment. Returns the new token (for the transport's 401 replay) or
   * null when the session is truly over. A failed refresh ends the session
   * rather than retrying.
   */
  const renewAccessToken = useCallback((): Promise<string | null> => {
    refreshInFlightRef.current ??= (async () => {
      try {
        const renewed = await refreshRequest();
        setUserId(renewed.user_id);
        setAccessToken(renewed.access_token);
        scheduleRefresh(renewed.expires_in);
        return renewed.access_token;
      } catch {
        resetToUnauthenticated();
        return null;
      } finally {
        refreshInFlightRef.current = null;
      }
    })();
    return refreshInFlightRef.current;
  }, [scheduleRefresh, resetToUnauthenticated]);

  // Assign during an effect, not render: writing a ref during render is a
  // render side effect (and StrictMode renders twice).
  useEffect(() => {
    renewRef.current = renewAccessToken;
  }, [renewAccessToken]);

  // The transport's 401 path replays a request once with the renewed token;
  // register the single-flight refresher for the provider's lifetime.
  useEffect(() => {
    setTokenRefresher(renewAccessToken);
    return () => setTokenRefresher(null);
  }, [renewAccessToken]);

  // A background tab's timers run late, so a token can already be expired
  // (or nearly) when the tab becomes visible again; refresh eagerly then.
  useEffect(() => {
    const onVisible = () => {
      if (document.hidden) return;
      const expiresAt = tokenExpiresAtRef.current;
      if (expiresAt !== null && Date.now() >= expiresAt - EXPIRY_WAKE_MARGIN_MS) {
        void renewRef.current();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, []);

  // Track the active workspace's access so the UI can present a
  // role-appropriate surface (see the `permissions` context field).
  useEffect(() => {
    if (status !== "AUTHENTICATED" || !accessToken || !workspaceId) {
      accessGenerationRef.current += 1;
      setPermissions(null);
      return;
    }
    const generation = (accessGenerationRef.current += 1);
    void (async () => {
      try {
        const workspace = await getWorkspace(workspaceId, accessToken);
        if (accessGenerationRef.current !== generation) return;
        setPermissions(workspace?.permissions ?? null);
      } catch {
        if (accessGenerationRef.current === generation) setPermissions(null);
      }
    })();
  }, [status, accessToken, workspaceId]);

  const adoptSession = useCallback(
    async (result: { user_id: string; access_token: string; expires_in?: number | null }) => {
      setSessionId((current) => current + 1);
      setUserId(result.user_id);
      setAccessToken(result.access_token);
      setStatus("AUTHENTICATED");
      scheduleRefresh(result.expires_in);
      const available = await loadTenancy(result.access_token);
      // A failed load keeps whatever selection is already remembered: a
      // network blip must not look like "the user has no workspaces".
      if (available !== null) selectInitialWorkspace(available);
    },
    [loadTenancy, scheduleRefresh, selectInitialWorkspace],
  );

  // Bootstrap: a missing or expired refresh cookie is a normal signed-out
  // visit, not an API error.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const result = await refreshRequest();
        if (cancelled) return;
        await adoptSession(result);
      } catch {
        if (!cancelled) {
          setStatus("UNAUTHENTICATED");
          setSessionId((current) => current + 1);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // Bootstrap runs once for the provider lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => clearRefreshTimer, [clearRefreshTimer]);

  const signIn = useCallback(
    async (input: { email: string; password: string }) => {
      const result = await loginRequest(input);
      await adoptSession(result);
    },
    [adoptSession],
  );

  const signUp = useCallback(
    async (input: { email: string; password: string }) => {
      const result = await registerRequest(input);
      await adoptSession(result);
    },
    [adoptSession],
  );

  const signOut = useCallback(async () => {
    try {
      await logoutRequest();
    } catch {
      // The local session is dropped regardless of the server's answer.
    }
    storeWorkspaceId("");
    resetToUnauthenticated();
  }, [resetToUnauthenticated]);

  const setActiveWorkspace = useCallback(
    (nextWorkspaceId: string) => {
      // Updaters must stay pure, so the decision happens here against the
      // live state and each commit is its own plain statement. The previous
      // version did the storage write and the generation bump *inside* the
      // updater, which StrictMode's double invocation executed twice.
      if (workspaceId !== nextWorkspaceId) {
        storeWorkspaceId(nextWorkspaceId);
        setSessionId((generation) => generation + 1);
        setWorkspaceId(nextWorkspaceId);
      }
      setPanelOpen(false);
    },
    [workspaceId],
  );

  const reloadTenancy = useCallback(async () => {
    if (!accessToken) return;
    const available = await loadTenancy(accessToken);
    if (available !== null && available.length > 0 && !available.some((workspace) => workspace.id === workspaceId)) {
      selectInitialWorkspace(available);
    }
  }, [accessToken, loadTenancy, selectInitialWorkspace, workspaceId]);

  const openPanel = useCallback(() => setPanelOpen(true), []);
  const closePanel = useCallback(() => setPanelOpen(false), []);

  const activeWorkspace = workspaces.find((workspace) => workspace.id === workspaceId) ?? null;
  const organizationId = activeWorkspace?.organization_id ?? "";
  const authenticated = status === "AUTHENTICATED";

  const value = useMemo<FrontendSessionContextValue>(
    () => ({
      status,
      userId,
      accessToken,
      organizations,
      workspaces,
      organizationId,
      workspaceId,
      permissions,
      sessionId,
      authenticated,
      connected: authenticated && Boolean(workspaceId && accessToken),
      tenancyLoading,
      tenancyError,
      signIn,
      signUp,
      signOut,
      setActiveWorkspace,
      reloadTenancy,
      panelOpen,
      openPanel,
      closePanel,
    }),
    [
      status,
      userId,
      accessToken,
      organizations,
      workspaces,
      organizationId,
      workspaceId,
      permissions,
      sessionId,
      authenticated,
      tenancyLoading,
      tenancyError,
      signIn,
      signUp,
      signOut,
      setActiveWorkspace,
      reloadTenancy,
      panelOpen,
      openPanel,
      closePanel,
    ],
  );

  return <FrontendSessionContext.Provider value={value}>{children}</FrontendSessionContext.Provider>;
}

export function useFrontendSession(): FrontendSessionContextValue {
  const value = useContext(FrontendSessionContext);
  if (!value) {
    throw new Error("useFrontendSession must be used inside FrontendSessionProvider.");
  }
  return value;
}

/** Auth input shared by every workspace-scoped client call. */
export function useWorkspaceAuth(): { workspaceId: string; accessToken: string } {
  const { workspaceId, accessToken } = useFrontendSession();
  return { workspaceId, accessToken };
}
