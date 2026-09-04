import { TranslatedText } from "@/components/TranslatedText";
import type { Metadata } from "next";

import { publicPageMetadata } from "@/lib/seo";

export const metadata: Metadata = publicPageMetadata(
  "Download",
  "Download options for JELICA Desktop and CLI are being prepared.",
  "/download",
);

export default function DownloadPage() {
  return (
    <section className="panel stack">
      <h1 style={{ margin: 0 }}><TranslatedText id="public.download.title" /></h1>
      <p className="muted" style={{ margin: 0 }}>
        <TranslatedText id="public.download.description" />
      </p>
      <div className="landing-grid">
        <article className="state-box">
          <strong><TranslatedText id="public.download.desktop-label" /></strong>
          <p className="muted" style={{ marginBottom: 0 }}><TranslatedText id="public.download.coming-soon" /></p>
        </article>
        <article className="state-box">
          <strong><TranslatedText id="public.download.cli-label" /></strong>
          <p className="muted" style={{ marginBottom: 0 }}><TranslatedText id="public.download.coming-soon" /></p>
        </article>
      </div>
    </section>
  );
}
