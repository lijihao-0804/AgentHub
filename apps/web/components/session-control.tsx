"use client";

import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import { useFrontendSession } from "./session-provider";

function shortWorkspaceId(value: string): string {
  if (value.length <= 8) return value;
  return `${value.slice(0, 4)}…${value.slice(-3)}`;
}

/**
 * Topbar session control. The access token is committed to the in-memory
 * provider on submit and never rendered, logged, or persisted.
 */
export default function SessionControl() {
  const { workspaceId, accessToken, connected, setSession, clearSession, panelOpen, openPanel, closePanel } =
    useFrontendSession();
  const [draftWorkspaceId, setDraftWorkspaceId] = useState("");
  const [draftAccessToken, setDraftAccessToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const workspaceInputId = useId();
  const tokenInputId = useId();
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!panelOpen) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") closePanel();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [panelOpen, closePanel]);

  function openWithDraft() {
    setDraftWorkspaceId(workspaceId);
    setDraftAccessToken("");
    setError(null);
    openPanel();
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!draftWorkspaceId.trim() || !draftAccessToken.trim()) {
      setError("Workspace ID and access token are both required.");
      return;
    }
    setSession(draftWorkspaceId, draftAccessToken);
    setDraftAccessToken("");
    setError(null);
  }

  if (connected) {
    return (
      <div className="session-control session-control-connected">
        <span className="session-status" data-state="active">
          <span className="session-dot" aria-hidden="true" />
          Workspace <code>{shortWorkspaceId(workspaceId)}</code>
          <span className="sr-only">Session active</span>
        </span>
        <button type="button" className="button button-ghost" onClick={openWithDraft}>
          Change
        </button>
        <button type="button" className="button button-ghost" onClick={clearSession}>
          Clear session
        </button>
        {panelOpen && (
          <div className="session-panel" ref={panelRef} role="group" aria-label="Workspace session">
            <form onSubmit={handleSubmit}>
              <label htmlFor={workspaceInputId}>Workspace ID</label>
              <input
                id={workspaceInputId}
                value={draftWorkspaceId}
                onChange={(event) => setDraftWorkspaceId(event.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
              <label htmlFor={tokenInputId}>Access token</label>
              <input
                id={tokenInputId}
                type="password"
                autoComplete="off"
                value={draftAccessToken}
                onChange={(event) => setDraftAccessToken(event.target.value)}
                placeholder="Enter a new token to replace the current session"
              />
              {error && <p className="session-error" role="alert">{error}</p>}
              <div className="session-panel-actions">
                <button type="submit" className="button button-primary">
                  Use session
                </button>
                <button type="button" className="button button-ghost" onClick={closePanel}>
                  Cancel
                </button>
              </div>
            </form>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="session-control">
      <button
        type="button"
        className="button button-primary"
        aria-expanded={panelOpen}
        aria-haspopup="dialog"
        onClick={openWithDraft}
      >
        <span className="session-dot" aria-hidden="true" />
        Connect workspace
      </button>
      {panelOpen && (
        <div className="session-panel" ref={panelRef} role="dialog" aria-label="Connect workspace">
          <form onSubmit={handleSubmit}>
            <label htmlFor={workspaceInputId}>Workspace ID</label>
            <input
              id={workspaceInputId}
              autoFocus
              value={draftWorkspaceId}
              onChange={(event) => setDraftWorkspaceId(event.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
            <label htmlFor={tokenInputId}>Access token</label>
            <input
              id={tokenInputId}
              type="password"
              autoComplete="off"
              value={draftAccessToken}
              onChange={(event) => setDraftAccessToken(event.target.value)}
              placeholder="Bearer access token"
            />
            {error && <p className="session-error" role="alert">{error}</p>}
            <div className="session-panel-actions">
              <button type="submit" className="button button-primary">
                Use session
              </button>
              <button type="button" className="button button-ghost" onClick={closePanel}>
                Cancel
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
