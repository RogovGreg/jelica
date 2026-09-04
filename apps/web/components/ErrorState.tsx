import type { ReactNode } from "react";

type ErrorStateProps = {
  title: ReactNode;
  description: ReactNode;
  children?: ReactNode;
};

export function ErrorState({ title, description, children }: ErrorStateProps) {
  return (
    <section className="panel stack" role="alert">
      <h1 style={{ margin: 0 }}>{title}</h1>
      <div className="state-box">{description}</div>
      {children}
    </section>
  );
}
