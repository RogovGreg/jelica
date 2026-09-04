import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: { default: "Documentation", template: "%s | JELICA" },
  robots: { index: false, follow: false },
};

export default function DocumentationLayout({ children }: Readonly<{ children: ReactNode }>) {
  return children;
}
