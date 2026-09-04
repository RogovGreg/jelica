/* eslint-disable @next/next/no-img-element */
import type { ReactNode } from "react";

import { isSafeExternalUrl } from "../../../../packages/app-platform/src/platform";

export type MarkdownBlock =
  | { type: "heading"; level: number; text: string }
  | { type: "paragraph"; text: string }
  | { type: "unordered-list"; items: string[] }
  | { type: "ordered-list"; items: string[] }
  | { type: "image"; alt: string; src: string };

export function parseMarkdown(source: string): readonly MarkdownBlock[] {
  const lines = source.replaceAll("\r\n", "\n").split("\n");
  const blocks: MarkdownBlock[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index].trim();
    if (line === "") {
      index += 1;
      continue;
    }
    const heading = /^(#{1,6})\s+(.+?)\s*#*$/.exec(line);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, text: heading[2] });
      index += 1;
      continue;
    }
    const image = /^!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)$/.exec(line);
    if (image) {
      blocks.push({ type: "image", alt: image[1], src: image[2] });
      index += 1;
      continue;
    }
    const unordered = /^(?:[-*])\s+(.+)$/.exec(line);
    if (unordered) {
      const items: string[] = [];
      while (index < lines.length) {
        const match = /^(?:[-*])\s+(.+)$/.exec(lines[index].trim());
        if (!match) break;
        items.push(match[1]);
        index += 1;
      }
      blocks.push({ type: "unordered-list", items });
      continue;
    }
    const ordered = /^\d+[.)]\s+(.+)$/.exec(line);
    if (ordered) {
      const items: string[] = [];
      while (index < lines.length) {
        const match = /^\d+[.)]\s+(.+)$/.exec(lines[index].trim());
        if (!match) break;
        items.push(match[1]);
        index += 1;
      }
      blocks.push({ type: "ordered-list", items });
      continue;
    }
    const paragraph: string[] = [];
    while (index < lines.length && lines[index].trim() !== "" && !isBlockStart(lines[index].trim())) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    blocks.push({ type: "paragraph", text: paragraph.join(" ") });
  }
  return blocks;
}

export function MarkdownContent({ source }: Readonly<{ source: string }>) {
  return <div className="markdown-content">{parseMarkdown(source).map(renderBlock)}</div>;
}

function renderBlock(block: MarkdownBlock, index: number): ReactNode {
  if (block.type === "heading") {
    const Heading = `h${block.level}` as keyof JSX.IntrinsicElements;
    return <Heading key={index}>{renderInline(block.text, `${index}-heading`)}</Heading>;
  }
  if (block.type === "paragraph") {
    return <p key={index}>{renderInline(block.text, `${index}-paragraph`)}</p>;
  }
  if (block.type === "unordered-list" || block.type === "ordered-list") {
    const List = block.type === "unordered-list" ? "ul" : "ol";
    return <List key={index}>{block.items.map((item, itemIndex) => <li key={itemIndex}>{renderInline(item, `${index}-${itemIndex}`)}</li>)}</List>;
  }
  const safeSource = safeMarkdownUrl(block.src);
  return safeSource ? <img key={index} src={safeSource} alt={block.alt} className="markdown-image" /> : null;
}

function renderInline(source: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(!?\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)|`([^`]+)`)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(source)) !== null) {
    if (match.index > lastIndex) nodes.push(source.slice(lastIndex, match.index));
    const token = match[0];
    if (token.startsWith("![")) {
      const safeSource = safeMarkdownUrl(match[3]);
      if (safeSource) nodes.push(<img key={`${keyPrefix}-${match.index}`} src={safeSource} alt={match[2]} className="markdown-inline-image" />);
    } else if (token.startsWith("[")) {
      const safeHref = safeMarkdownUrl(match[3]);
      nodes.push(safeHref ? <a key={`${keyPrefix}-${match.index}`} href={safeHref} target={isSafeExternalUrl(safeHref) ? "_blank" : undefined} rel={isSafeExternalUrl(safeHref) ? "noreferrer" : undefined}>{match[2]}</a> : match[2]);
    } else {
      nodes.push(<code key={`${keyPrefix}-${match.index}`}>{match[4]}</code>);
    }
    lastIndex = match.index + token.length;
  }
  if (lastIndex < source.length) nodes.push(source.slice(lastIndex));
  return nodes;
}

function isBlockStart(line: string): boolean {
  return /^(#{1,6})\s+/.test(line) || /^(?:[-*])\s+/.test(line) || /^\d+[.)]\s+/.test(line) || /^!\[[^\]]*\]\([^)\s]+(?:\s+"[^"]*")?\)$/.test(line);
}

function safeMarkdownUrl(rawUrl: string): string | null {
  if (rawUrl.startsWith("/") && !rawUrl.startsWith("//") && !/[\\\u0000-\u001f\u007f]/.test(rawUrl)) return rawUrl;
  if (rawUrl.startsWith("#") && !/[\\\u0000-\u001f\u007f]/.test(rawUrl)) return rawUrl;
  return isSafeExternalUrl(rawUrl) ? rawUrl : null;
}
