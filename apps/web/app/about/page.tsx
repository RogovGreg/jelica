import { cookies } from "next/headers";

import { TranslatedText } from "@/components/TranslatedText";
import { MarkdownContent } from "@/lib/content/markdown";
import { loadAbout } from "@/lib/content/about";
import { JELICA_LOCALE_COOKIE } from "@/lib/documentation/request";
import { resolveLocale } from "@/lib/i18n";
import type { Metadata } from "next";

import { publicPageMetadata } from "@/lib/seo";

export const metadata: Metadata = publicPageMetadata(
  "About",
  "Learn about JELICA’s comparative genomics platform and reproducible analytical workflow.",
  "/about",
);

export default function AboutPage() {
  const article = loadAbout(resolveLocale(cookies().get(JELICA_LOCALE_COOKIE)?.value));
  return (
    <section className="panel stack">
      <h1 style={{ margin: 0 }}><TranslatedText id="public.about.title" /></h1>
      {article ? <MarkdownContent source={article} /> : <p className="state-box"><TranslatedText id="public.about.unavailable" /></p>}
    </section>
  );
}
