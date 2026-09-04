import Link from "next/link";
import { cookies } from "next/headers";

import { listNews } from "@/lib/content/news";
import { TranslatedText } from "@/components/TranslatedText";
import { JELICA_LOCALE_COOKIE } from "@/lib/documentation/request";
import { resolveLocale } from "@/lib/i18n";
import type { Metadata } from "next";

import { publicPageMetadata } from "@/lib/seo";

export const metadata: Metadata = publicPageMetadata(
  "News",
  "News and project updates from JELICA.",
  "/news",
);

export default function NewsPage() {
  const items = listNews(resolveLocale(cookies().get(JELICA_LOCALE_COOKIE)?.value));
  return (
    <section className="panel stack">
      <div>
        <h1 style={{ margin: 0 }}><TranslatedText id="public.news.title" /></h1>
        <p className="muted" style={{ marginTop: "0.35rem" }}>
          <TranslatedText id="public.news.description" />
        </p>
      </div>
      {items.length === 0 ? <p className="state-box"><TranslatedText id="public.news.empty" /></p> : <div className="landing-grid">
        {items.map((item) => (
          <article key={item.slug} className="state-box stack" style={{ gap: "0.45rem" }}>
            <p className="muted" style={{ margin: 0 }}>
              {item.date}
            </p>
            <h2 style={{ margin: 0 }}>{item.title}</h2>
            <p className="muted" style={{ margin: 0 }}>
              {item.summary}
            </p>
            <div>
              <Link href={`/news/${item.slug}`}><TranslatedText id="public.news.read" /></Link>
            </div>
          </article>
        ))}
      </div>}
    </section>
  );
}
