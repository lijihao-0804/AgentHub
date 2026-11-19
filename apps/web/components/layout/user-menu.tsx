"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { useI18n } from "@/i18n/provider";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useMenuKeyboardNav } from "@/components/layout/menu-keyboard";

function shortUserId(value: string): string {
  if (value.length <= 10) return value;
  return `${value.slice(0, 6)}…${value.slice(-4)}`;
}

const UserGlyph = (
  <svg aria-hidden="true" fill="none" viewBox="0 0 20 20">
    <circle cx="10" cy="7.2" r="3" stroke="currentColor" strokeWidth="1.5" />
    <path
      d="M4.8 16.2c.55-2.35 2.5-3.8 5.2-3.8s4.65 1.45 5.2 3.8"
      stroke="currentColor"
      strokeLinecap="round"
      strokeWidth="1.5"
    />
  </svg>
);

/** Topbar identity menu: who is signed in, and how to sign out. */
export default function UserMenu() {
  const { t } = useI18n();
  const { userId, signOut } = useFrontendSession();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const containerRef = useMenuKeyboardNav(open);

  useEffect(() => {
    if (!open) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        containerRef.current?.querySelector<HTMLButtonElement>("button[aria-haspopup]")?.focus();
        setOpen(false);
      }
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
    <div className="user-menu" ref={containerRef}
      onBlur={(event) => { if (open && event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) setOpen(false); }}>
      <button
        type="button"
        className="user-menu-trigger"
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label={`${t("user.account")}: ${shortUserId(userId)}`}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="user-avatar">{UserGlyph}</span>
        <span className="user-label">{t("user.account")}</span>
      </button>
      {open && (
        <div className="ws-menu" role="menu" aria-label={t("user.account")}>
          <p className="ws-menu-group">{t("user.signedInAs")}</p>
          <p className="user-email">
            <code>{shortUserId(userId)}</code>
          </p>
          <div className="ws-menu-divider" />
          <Link
            role="menuitem"
            className="ws-menu-item"
            href="/settings#appearance"
            onClick={() => setOpen(false)}
          >
            {t("appearance.title")}
          </Link>
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
