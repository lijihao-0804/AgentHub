"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { useI18n } from "../i18n/provider";
import { useFrontendSession } from "./session-provider";

function shortUserId(value: string): string {
  if (value.length <= 10) return value;
  return `${value.slice(0, 6)}…${value.slice(-4)}`;
}

/** Topbar identity menu: who is signed in, and how to sign out. */
export default function UserMenu() {
  const { t } = useI18n();
  const { userId, signOut } = useFrontendSession();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    function onPointerDown(event: MouseEvent) {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onPointerDown);
    };
  }, [open]);

  async function handleSignOut() {
    setSigningOut(true);
    await signOut();
    setOpen(false);
    setSigningOut(false);
    router.push("/login");
  }

  return (
    <div className="user-menu" ref={containerRef}>
      <button
        type="button"
        className="button button-ghost"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((value) => !value)}
      >
        {t("user.account")}
      </button>
      {open && (
        <div className="ws-menu" role="menu" aria-label={t("user.account")}>
          <p className="ws-menu-group">{t("user.signedInAs")}</p>
          <p className="user-email">
            <code>{shortUserId(userId)}</code>
          </p>
          <div className="ws-menu-divider" />
          <button
            type="button"
            role="menuitem"
            className="ws-menu-item"
            onClick={() => void handleSignOut()}
            disabled={signingOut}
          >
            {signingOut ? t("common.loading") : t("user.signOut")}
          </button>
        </div>
      )}
    </div>
  );
}
