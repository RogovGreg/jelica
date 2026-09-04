"use client";

import { useEffect, useRef } from "react";

import { useI18n } from "@/components/I18nProvider";
type ConfirmDialogProps = Readonly<{
  title: string;
  description: string;
  actionLabel: string;
  destructive?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}>;

export function ConfirmDialog({
  title,
  description,
  actionLabel,
  destructive = false,
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const { t } = useI18n();
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    cancelRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onCancel();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [busy, onCancel]);

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && !busy && onCancel()}>
      <section className="dialog panel stack" role="dialog" aria-modal="true" aria-labelledby="confirm-dialog-title">
        <h2 id="confirm-dialog-title" style={{ margin: 0 }}>{title}</h2>
        <p className="muted" style={{ margin: 0 }}>{description}</p>
        <div className="actions-row">
          <button type="button" className="secondary-button" onClick={onCancel} disabled={busy} ref={cancelRef}>{t("project.action.cancel")}</button>
          <button type="button" className={destructive ? "danger-button" : "primary-button"} onClick={onConfirm} disabled={busy}>
            {busy ? t("project.action.working") : actionLabel}
          </button>
        </div>
      </section>
    </div>
  );
}
