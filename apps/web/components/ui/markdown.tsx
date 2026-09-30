"use client";

import { Fragment, type ReactNode } from "react";

/**
 * A deliberately small, dependency-free markdown renderer for model output.
 *
 * The input is never trusted: text is escaped character-for-character and the
 * result is built as React nodes, so no `dangerouslySetInnerHTML` exists on
 * the path. Supported: headings (#..###), bullet and numbered lists,
 * blockquotes, fenced code blocks (with copy), inline code, bold, italic and
 * http(s) links. Everything else renders as plain text.
 */

function escapeText(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

type Token = { kind: "text" | "code" | "bold" | "italic" | "link"; value: string; href?: string };

const INLINE_PATTERN =
  /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\n]+\*)|(\[[^\]]+\]\((?:https?:\/\/)[^\s)]+\))/g;

function tokenizeInline(escaped: string): Token[] {
  const tokens: Token[] = [];
  let lastIndex = 0;
  for (const match of escaped.matchAll(INLINE_PATTERN)) {
    const index = match.index ?? 0;
    if (index > lastIndex) tokens.push({ kind: "text", value: escaped.slice(lastIndex, index) });
    const raw = match[0];
    if (raw.startsWith("`")) tokens.push({ kind: "code", value: raw.slice(1, -1) });
    else if (raw.startsWith("**")) tokens.push({ kind: "bold", value: raw.slice(2, -2) });
    else if (raw.startsWith("*")) tokens.push({ kind: "italic", value: raw.slice(1, -1) });
    else {
      const label = raw.slice(1, raw.indexOf("]"));
      const href = raw.slice(raw.indexOf("](") + 2, -1);
      tokens.push({ kind: "link", value: label, href });
    }
    lastIndex = index + raw.length;
  }
  if (lastIndex < escaped.length) tokens.push({ kind: "text", value: escaped.slice(lastIndex) });
  return tokens;
}

function renderInline(escaped: string, keyPrefix: string): ReactNode[] {
  return tokenizeInline(escaped).map((token, index) => {
    const key = `${keyPrefix}-${index}`;
    switch (token.kind) {
      case "code":
        return <code key={key}>{token.value}</code>;
      case "bold":
        return <strong key={key}>{token.value}</strong>;
      case "italic":
        return <em key={key}>{token.value}</em>;
      case "link":
        return (
          <a key={key} href={token.href} target="_blank" rel="noreferrer noopener">
            {token.value}
          </a>
        );
      default:
        return <Fragment key={key}>{token.value}</Fragment>;
    }
  });
}

/** Copy button for fenced blocks; feedback is local and self-clearing. */
function CodeBlock({ code }: { code: string }) {
  return (
    <div className="md-code">
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  );
}

export function Markdown({ text }: { text: string }) {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let quote: string[] = [];
  let code: { lang: string; lines: string[] } | null = null;
  let key = 0;

  const flushParagraph = () => {
    if (paragraph.length === 0) return;
    const escaped = escapeText(paragraph.join(" "));
    blocks.push(<p key={`p-${key++}`}>{renderInline(escaped, `p-${key}`)}</p>);
    paragraph = [];
  };
  const flushList = () => {
    if (!list) return;
    const escapedItems = list.items.map((item) => renderInline(escapeText(item), `li-${key}`));
    blocks.push(
      list.ordered ? (
        <ol key={`ol-${key++}`}>{escapedItems.map((node, index) => <li key={index}>{node}</li>)}</ol>
      ) : (
        <ul key={`ul-${key++}`}>{escapedItems.map((node, index) => <li key={index}>{node}</li>)}</ul>
      ),
    );
    list = null;
  };
  const flushQuote = () => {
    if (quote.length === 0) return;
    blocks.push(
      <blockquote key={`q-${key++}`}>
        {renderInline(escapeText(quote.join(" ")), `q-${key}`)}
      </blockquote>,
    );
    quote = [];
  };
  const flushAll = () => {
    flushParagraph();
    flushList();
    flushQuote();
  };

  for (const line of lines) {
    if (code !== null) {
      if (line.trim().startsWith("```")) {
        blocks.push(<CodeBlock key={`c-${key++}`} code={code.lines.join("\n")} />);
        code = null;
      } else {
        code.lines.push(line);
      }
      continue;
    }
    if (line.trim().startsWith("```")) {
      flushAll();
      code = { lang: line.trim().slice(3), lines: [] };
      continue;
    }
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      flushAll();
      const level = heading[1].length;
      const content = renderInline(escapeText(heading[2]), `h-${key}`);
      blocks.push(
        level === 1 ? <h3 key={`h-${key++}`}>{content}</h3> : level === 2 ? <h4 key={`h-${key++}`}>{content}</h4> : <h5 key={`h-${key++}`}>{content}</h5>,
      );
      continue;
    }
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    if (bullet) {
      flushParagraph();
      flushQuote();
      if (!list || list.ordered) list = { ordered: false, items: [] };
      list.items.push(bullet[1]);
      continue;
    }
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (numbered) {
      flushParagraph();
      flushQuote();
      if (!list || !list.ordered) list = { ordered: true, items: [] };
      list.items.push(numbered[1]);
      continue;
    }
    const blockquote = /^>\s?(.*)$/.exec(line);
    if (blockquote) {
      flushParagraph();
      flushList();
      quote.push(blockquote[1]);
      continue;
    }
    if (line.trim() === "") {
      flushAll();
      continue;
    }
    flushList();
    flushQuote();
    paragraph.push(line.trim());
  }
  if (code !== null) blocks.push(<CodeBlock key={`c-${key++}`} code={code.lines.join("\n")} />);
  flushAll();

  return <div className="md-body">{blocks}</div>;
}
