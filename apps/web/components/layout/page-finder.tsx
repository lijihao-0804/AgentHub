"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { usePathname } from "next/navigation";

import Icon, { type IconName } from "@/components/ui/icon";
import { useI18n } from "@/i18n/provider";
import type { MessageKey } from "@/i18n/messages";

type Destination = { href: string; labelKey: MessageKey; icon: IconName };
type Group = { headingKey: MessageKey; items: Destination[] };

export default function PageFinder({ groups }: { groups: Group[] }) {
  const { t, locale } = useI18n();
  const pathname = usePathname();
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const zh = locale === "zh-CN";
  const label = zh ? "查找页面" : "Find a page";

  function open() {
    setQuery("");
    if (!dialog.current?.open) dialog.current?.showModal();
    input.current?.focus();
  }

  useEffect(() => {
    function shortcut(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey &&
          !event.isComposing && event.key.toLowerCase() === "k") {
        if (document.body.classList.contains("nav-open")) return;
        if (document.querySelector("dialog[open]") && !dialog.current?.open) return;
        event.preventDefault();
        open();
      }
    }
    document.addEventListener("keydown", shortcut);
    return () => document.removeEventListener("keydown", shortcut);
  }, []);

  useEffect(() => { dialog.current?.close(); }, [pathname]);
  const term = query.trim().toLowerCase();
  const matches = groups.map(({ headingKey, items }) => ({
    headingKey,
    items: items.filter(({ href, labelKey }) =>
      [href, t(labelKey), t(headingKey)].some((text) => text.toLowerCase().includes(term))),
  })).filter(({ items }) => items.length);

  return (
    <>
      <button className="finder-trigger shell-control" type="button" onClick={open}
        title={`${label} (Ctrl / ⌘ K)`} aria-label={label} aria-haspopup="dialog" aria-keyshortcuts="Control+K Meta+K">
        <Icon name="search" size={16} /><span>{label}</span><kbd aria-hidden="true">Ctrl / ⌘ K</kbd>
      </button>
      <dialog ref={dialog} className="page-finder" aria-label={label}
        onClick={(event) => { if (event.target === dialog.current) dialog.current.close(); }}>
        <div className="finder-body" onKeyDown={(event) => {
          if (event.nativeEvent.isComposing) return;
          const links = Array.from(event.currentTarget.querySelectorAll<HTMLAnchorElement>("a"));
          const active = links.indexOf(document.activeElement as HTMLAnchorElement);
          if (links.length && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
            event.preventDefault();
            const next = event.key === "ArrowDown" ? active + 1 : active < 0 ? links.length - 1 : active - 1;
            links[(next + links.length) % links.length].focus();
          } else if (event.key === "Enter" && event.target === input.current && links[0]) {
            event.preventDefault();
            links[0].click();
          }
        }}>
          <div className="finder-input">
            <Icon name="search" size={20} />
            <input ref={input} type="search" value={query} aria-label={label}
              placeholder={zh ? "输入页面名称或路径" : "Type a page name or path"}
              onChange={(event) => setQuery(event.target.value)} />
            <button type="button" onClick={() => dialog.current?.close()}
              aria-label={zh ? "关闭搜索" : "Close search"}>Esc</button>
          </div>
          <nav className="finder-links" aria-label={label}>
            {matches.map(({ headingKey, items }) => (
              <section key={headingKey}>
                <h2>{t(headingKey)}</h2>
                {items.map((item) => (
                  <Link href={item.href} key={item.href} onClick={() => dialog.current?.close()}>
                    <Icon name={item.icon} size={18} />{t(item.labelKey)}
                    <span>{item.href}</span>
                  </Link>
                ))}
              </section>
            ))}
            {!matches.length && <p role="status">{zh ? "没有匹配的页面" : "No matching pages"}</p>}
          </nav>
        </div>
      </dialog>
    </>
  );
}
