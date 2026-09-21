import type { MessageKey } from "@/i18n/messages";
import type { ThreadKind } from "@/lib/api/threads";

/**
 * Everything that distinguishes one application's thread pages from another's.
 *
 * The claim the four applications are supposed to prove is that they are one
 * runtime in four configurations. That claim has to survive contact with the
 * frontend too, so the list page, the thread rail and the conversation pane are
 * one component each, and an application supplies this record rather than a
 * copy of them. An application that cannot be expressed as a `AppThreadCopy`
 * plus its own artifact panes is an application that broke the abstraction.
 */
export type AppThreadCopy = {
  /** The routing label written on threads this application creates. */
  kind: ThreadKind;
  /** Where the list page lives; a thread is `${basePath}/${id}`. */
  basePath: string;
  eyebrow: MessageKey;
  title: MessageKey;
  lede: MessageKey;
  newThread: MessageKey;
  createTitle: MessageKey;
  threadTitlePlaceholder: MessageKey;
  recent: MessageKey;
  noThreads: MessageKey;
  noThreadsHint: MessageKey;
  /** Explains to a disconnected visitor what the session would show them. */
  sessionContext: MessageKey;
};
