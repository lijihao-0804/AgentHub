"use client";

import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import { useI18n } from "../i18n/provider";
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
  const { t } = useI18n();
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
      setError(t("session.requiredFields"));
      return;
    }
    setSession(draftWorkspaceId, draftAccessToken);
    setDraftAccessToken("");
    setError(null);
  }

  const panel = (
    <div className="session-panel" ref={panelRef} role="group" aria-label={t("session.panelSession")}>
      <form onSubmit={handleSubmit}>
        <label htmlFor={workspaceInputId}>{t("session.workspaceId")}</label>
        <input
          id={workspaceInputId}
          value={draftWorkspaceId}
          onChange={(event) => setDraftWorkspaceId(event.target.value)}
          autoComplete="off"
          spellCheck={false}
        />
        <label htmlFor={tokenInputId}>{t("session.accessToken")}</label>
        <input
          id={tokenInputId}
          type="password"
          autoComplete="off"
          value={draftAccessToken}
          onChange={(event) => setDraftAccessToken(event.target.value)}
          placeholder={connected ? t("session.replaceTokenPlaceholder") : t("session.tokenPlaceholder")}
        />
        {error && <p className="session-error" role="alert">{error}</p>}
        <div className="session-panel-actions">
          <button type="submit" className="button button-primary">
            {t("session.use")}
          </button>
          <button type="button" className="button button-ghost" onClick={closePanel}>
            {t("session.cancel")}
          </button>
        </div>
      </form>
    </div>
  );

  if (connected) {
    return (
      <div className="session-control session-control-connected">
        <span className="session-status" data-state="active">
          <span className="session-dot" aria-hidden="true" />
          <span className="session-status-label">{t("session.workspaceLabel")}</span>{" "}
          <code>{shortWorkspaceId(workspaceId)}</code>
          <span className="sr-only">{t("session.active")}</span>
        </span>
        <button type="button" className="button button-ghost" onClick={openWithDraft}>
          {t("session.change")}
        </button>
        <button type="button" className="button button-ghost" onClick={clearSession}>
          {t("session.clear")}
        </button>
        {panelOpen && panel}
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
        {t("session.connect")}
      </button>
      {panelOpen && (
        <div className="session-panel" ref={panelRef} role="dialog" aria-label={t("session.panelConnect")}>
          <form onSubmit={handleSubmit}>
            <label htmlFor={workspaceInputId}>{t("session.workspaceId")}</label>
            <input
              id={workspaceInputId}
              autoFocus
              value={draftWorkspaceId}
              onChange={(event) => setDraftWorkspaceId(event.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
            <label htmlFor={tokenInputId}>{t("session.accessToken")}</label>
            <input
              id={tokenInputId}
              type="password"
              autoComplete="off"
              value={draftAccessToken}
              onChange={(event) => setDraftAccessToken(event.target.value)}
              placeholder={t("session.tokenPlaceholder")}
            />
            {error && <p className="session-error" role="alert">{error}</p>}
            <div className="session-panel-actions">
              <button type="submit" className="button button-primary">
                {t("session.use")}
              </button>
              <button type="button" className="button button-ghost" onClick={closePanel}>
                {t("session.cancel")}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
