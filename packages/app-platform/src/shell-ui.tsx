import { useEffect, useRef, type ReactNode } from "react";

import { isEditableCommandTarget, type ShellBreadcrumb } from "./shell";

type ShellPageHeaderProps = Readonly<{
  breadcrumbs: readonly ShellBreadcrumb[];
  breadcrumbLabel: string;
  commandLabel: string;
  commandPlaceholder: string;
  menuControl: ReactNode;
  renderBreadcrumbLink: (item: ShellBreadcrumb) => ReactNode;
}>;

export function ShellPageHeader({
  breadcrumbs,
  breadcrumbLabel,
  commandLabel,
  commandPlaceholder,
  menuControl,
  renderBreadcrumbLink,
}: ShellPageHeaderProps) {
  const commandRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const focusCommand = (event: KeyboardEvent) => {
      if (
        event.key !== "/" ||
        event.defaultPrevented ||
        event.metaKey ||
        event.ctrlKey ||
        event.altKey ||
        isEditableCommandTarget(event.target)
      ) {
        return;
      }
      event.preventDefault();
      commandRef.current?.focus();
    };
    window.addEventListener("keydown", focusCommand);
    return () => window.removeEventListener("keydown", focusCommand);
  }, []);

  return (
    <header className="shell-page-header">
      {menuControl}
      {breadcrumbs.length ? (
        <nav className="shell-breadcrumbs" aria-label={breadcrumbLabel}>
          <ol>
            {breadcrumbs.map((item, index) => (
              <li key={`${item.label}-${index}`}>
                {item.href ? renderBreadcrumbLink(item) : <span aria-current="page">{item.label}</span>}
              </li>
            ))}
          </ol>
        </nav>
      ) : <span className="shell-page-header-spacer" />}
      <label className="shell-command-input">
        <span className="visually-hidden">{commandLabel}</span>
        <span className="shell-command-icon" aria-hidden="true">⌕</span>
        <input ref={commandRef} type="text" placeholder={commandPlaceholder} />
        <kbd aria-hidden="true">/</kbd>
      </label>
    </header>
  );
}
