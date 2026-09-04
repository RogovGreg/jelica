import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

import type { DesktopSelection, DesktopSelectionKind } from "../common/contracts";

type RegisteredSelection = DesktopSelection & Readonly<{ nativePath: string }>;

export class SelectionRegistry {
  readonly #items = new Map<string, RegisteredSelection>();

  register(nativePath: string, kind: DesktopSelectionKind): DesktopSelection {
    const normalized = path.resolve(nativePath);
    const existing = [...this.#items.values()].find((item) => item.nativePath === normalized && item.kind === kind);
    if (existing) return { id: existing.id, kind: existing.kind, displayName: existing.displayName };
    const baseName = path.basename(normalized) || normalized;
    const usedNames = new Set([...this.#items.values()].map((item) => item.displayName));
    let displayName = baseName;
    let suffix = 2;
    while (usedNames.has(displayName)) displayName = `${baseName} (${suffix++})`;
    const item: RegisteredSelection = {
      id: crypto.randomUUID(),
      kind,
      displayName,
      nativePath: normalized,
    };
    this.#items.set(item.id, item);
    return { id: item.id, kind: item.kind, displayName: item.displayName };
  }

  resolve(selectionId: string, expectedKind: DesktopSelectionKind): string {
    const item = this.#items.get(selectionId);
    if (!item || item.kind !== expectedKind) throw new SelectionUnavailableError();
    try {
      const stat = fs.statSync(item.nativePath);
      const valid = expectedKind === "directory" ? stat.isDirectory() : stat.isFile();
      if (!valid) throw new Error("wrong type");
    } catch {
      throw new SelectionUnavailableError();
    }
    return item.nativePath;
  }

  kindOf(selectionId: string): DesktopSelectionKind {
    const item = this.#items.get(selectionId);
    if (!item) throw new SelectionUnavailableError();
    return item.kind;
  }

  remove(selectionId: string): boolean {
    return this.#items.delete(selectionId);
  }
}

export class SelectionUnavailableError extends Error {
  constructor() {
    super("The selected local input is no longer available.");
    this.name = "SelectionUnavailableError";
  }
}
