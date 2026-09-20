"use client";

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

/**
 * Frontend session state lives in React state only (NO TOKEN STORAGE).
 * A browser refresh unmounts the provider and clears the workspace
 * session; credentials are never written to storage, cookies or URLs.
 */
type FrontendSessionContextValue = {
  workspaceId: string;
  accessToken: string;
  /** Monotonic identity for the in-memory workspace + credential session. */
  sessionId: number;
  connected: boolean;
  setSession: (workspaceId: string, accessToken: string) => void;
  clearSession: () => void;
  panelOpen: boolean;
  openPanel: () => void;
  closePanel: () => void;
};

const FrontendSessionContext = createContext<FrontendSessionContextValue | null>(null);

export function FrontendSessionProvider({ children }: { children: ReactNode }) {
  const [workspaceId, setWorkspaceId] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [sessionId, setSessionId] = useState(0);
  const [panelOpen, setPanelOpen] = useState(false);

  const setSession = useCallback((nextWorkspaceId: string, nextAccessToken: string) => {
    setSessionId((current) => current + 1);
    setWorkspaceId(nextWorkspaceId.trim());
    setAccessToken(nextAccessToken.trim());
    setPanelOpen(false);
  }, []);

  const clearSession = useCallback(() => {
    setSessionId((current) => current + 1);
    setWorkspaceId("");
    setAccessToken("");
    setPanelOpen(false);
  }, []);

  const openPanel = useCallback(() => setPanelOpen(true), []);
  const closePanel = useCallback(() => setPanelOpen(false), []);

  const value = useMemo<FrontendSessionContextValue>(
    () => ({
      workspaceId,
      accessToken,
      sessionId,
      connected: Boolean(workspaceId.trim() && accessToken.trim()),
      setSession,
      clearSession,
      panelOpen,
      openPanel,
      closePanel,
    }),
    [workspaceId, accessToken, sessionId, setSession, clearSession, panelOpen, openPanel, closePanel],
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
