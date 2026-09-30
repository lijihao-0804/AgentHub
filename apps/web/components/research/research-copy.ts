import type { AppThreadCopy } from "@/components/apps/app-thread-copy";

/** The research workbench is the applications line with papers in the third
 * column; its copy record is all that distinguishes it from support. */
export const RESEARCH_COPY: AppThreadCopy = {
  kind: "research",
  basePath: "/research",
  eyebrow: "research.eyebrow",
  title: "research.title",
  lede: "research.lede",
  newThread: "research.newThread",
  createTitle: "research.createTitle",
  threadTitlePlaceholder: "research.threadTitlePlaceholder",
  recent: "research.recent",
  noThreads: "research.noThreads",
  noThreadsHint: "research.noThreadsHint",
  sessionContext: "session.context.research",
};
