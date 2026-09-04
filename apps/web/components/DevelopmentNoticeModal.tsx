"use client";

import { useEffect, useRef, useState } from "react";

export function DevelopmentNoticeModal() {
  const [open, setOpen] = useState(true);
  const okButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    okButtonRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
      } else if (event.key === "Tab") {
        event.preventDefault();
        okButtonRef.current?.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  if (!open) return null;
  return (
    <div className="development-notice-backdrop" role="presentation">
      <section className="development-notice panel stack" role="dialog" aria-modal="true" aria-label="JELICA Web development notice">
        <p>
          JELICA Web is currently being configured and actively developed.<br />
          Some features may be unavailable or work incorrectly.
        </p>
        <p lang="sr-Latn">
          JELICA Web se trenutno podešava i aktivno razvija.<br />
          Neke funkcionalnosti mogu biti nedostupne ili raditi nepravilno.
        </p>
        <p lang="ru">
          Веб-приложение JELICA сейчас находится в процессе настройки<br />
          и активной доработки.<br />
          Некоторые функции могут быть недоступны или работать некорректно.
        </p>
        <button ref={okButtonRef} type="button" className="primary-button" onClick={() => setOpen(false)}>OK</button>
      </section>
    </div>
  );
}
