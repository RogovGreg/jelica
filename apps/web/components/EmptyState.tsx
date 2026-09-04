import type { ReactNode } from "react";

type EmptyStateProps = {
  title: ReactNode;
  description: ReactNode;
  children?: ReactNode;
};

export function EmptyState({ title, description, children }: EmptyStateProps) {
  return (
    <section className="panel stack">
      <h1 style={{ margin: 0 }}>{title}</h1>
      <div className="state-box">{description}</div>
      {children}
    </section>
  );
}
