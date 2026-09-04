import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "License",
  description: "JELICA source-available licensing terms.",
  alternates: { canonical: "/license" },
};

export default function LicensePage() {
  return (
    <section className="panel stack">
      <h1 style={{ margin: 0 }}>License</h1>
      <div className="state-box">
        JELICA is source-available under the PolyForm Noncommercial License 1.0.0.
        <p style={{ marginBottom: 0 }}>
          See the repository <code>LICENSE</code> file for the complete terms and
          <a href="https://polyformproject.org/licenses/noncommercial/1.0.0"> the official license text</a>.
        </p>
      </div>
    </section>
  );
}
