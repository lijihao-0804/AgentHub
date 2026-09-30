"use client";

import { usePathname } from "next/navigation";
import { useEffect } from "react";

import { useI18n } from "@/i18n/provider";
import type { MessageKey } from "@/i18n/messages";

/**
 * Longest-prefix route → document title. Order matters: deeper prefixes must
 * come first. The server-rendered metadata keeps the plain product name; this
 * effect refines it per route so browser history and tabs distinguish pages
 * ("Runs · AgentHub"), which per-route `generateMetadata` in a client shell
 * cannot.
 */
const TITLE_RULES: Array<[string, MessageKey]> = [
  ["/runs/compare", "nav.runCompare"],
  ["/runs", "nav.runs"],
  ["/approvals", "nav.approvals"],
  ["/dashboard", "nav.dashboard"],
  ["/research", "nav.research"],
  ["/incidents", "nav.incidents"],
  ["/analytics", "nav.analytics"],
  ["/support", "nav.support"],
  ["/agents", "nav.agents"],
  ["/knowledge/playground", "nav.retrievalPlayground"],
  ["/knowledge", "nav.knowledge"],
  ["/tools", "nav.tools"],
  ["/evaluations/datasets", "nav.datasets"],
  ["/evaluations/experiments", "nav.experiments"],
  ["/evaluations/release-gates", "nav.releaseGates"],
  ["/evaluations/pricing", "nav.pricing"],
  ["/evaluations", "nav.evaluations"],
  ["/settings/models", "nav.models"],
  ["/settings", "nav.settings"],
  ["/login", "auth.loginTitle"],
  ["/register", "auth.registerTitle"],
];

export function RouteTitle() {
  const { t } = useI18n();
  const pathname = usePathname() ?? "";
  useEffect(() => {
    const rule = TITLE_RULES.find(
      ([prefix]) => pathname === prefix || pathname.startsWith(`${prefix}/`),
    );
    document.title = rule ? `${t(rule[1])} · AgentHub` : "AgentHub";
  }, [pathname, t]);
  return null;
}
