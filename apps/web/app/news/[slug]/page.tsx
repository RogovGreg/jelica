import Link from "next/link";
import { cookies } from "next/headers";
import { notFound } from "next/navigation";

import { MarkdownContent } from "@/lib/content/markdown";
import { getNewsBySlug } from "@/lib/content/news";
import { JELICA_LOCALE_COOKIE } from "@/lib/documentation/request";
import { resolveLocale } from "@/lib/i18n";
import { TranslatedText } from "@/components/TranslatedText";
import type { Metadata } from "next";

import { publicPageMetadata } from "@/lib/seo";

type NewsDetailsPageProps = {
  params: {
    slug: string;
  };
};

export function generateMetadata({ params }: NewsDetailsPageProps): Metadata {
  const article = getNewsBySlug(decodeSlug(params.slug), "en");
  if (article === null) return {};
  return publicPageMetadata(article.title, article.summary, `/news/${article.slug}`);
}

export default function NewsDetailsPage({ params }: NewsDetailsPageProps) {
  let slug: string;
  try {
    slug = decodeSlug(params.slug);
  } catch {
    notFound();
  }
  const article = getNewsBySlug(slug, resolveLocale(cookies().get(JELICA_LOCALE_COOKIE)?.value));
  if (article === null) {
    notFound();
  }

  return (
    <article className="panel stack">
      <div className="stack" style={{ gap: "0.45rem" }}>
        <p className="muted" style={{ margin: 0 }}>
          {article.date}
        </p>
        <h1 style={{ margin: 0 }}>{article.title}</h1>
        <p className="muted" style={{ margin: 0 }}>
          {article.summary}
        </p>
      </div>
      <MarkdownContent source={article.content} />
      <div className="actions-row">
        <Link href="/news" className="secondary-button">
          <TranslatedText id="public.news.back" />
        </Link>
      </div>
    </article>
  );
}

function decodeSlug(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}
