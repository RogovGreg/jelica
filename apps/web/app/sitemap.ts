import type { MetadataRoute } from "next";

import { listNews } from "@/lib/content/news";
import { siteUrl } from "@/lib/seo";

export default function sitemap(): MetadataRoute.Sitemap {
  const publicPages: MetadataRoute.Sitemap = [
    { url: siteUrl("/"), changeFrequency: "monthly", priority: 1 },
    { url: siteUrl("/about"), changeFrequency: "monthly", priority: 0.7 },
    { url: siteUrl("/news"), changeFrequency: "weekly", priority: 0.8 },
    { url: siteUrl("/download"), changeFrequency: "monthly", priority: 0.6 },
  ];
  const newsPages = listNews("en").map((article) => ({
    url: siteUrl(`/news/${article.slug}`),
    lastModified: new Date(`${article.date}T00:00:00Z`),
    changeFrequency: "monthly" as const,
    priority: 0.7,
  }));
  return [...publicPages, ...newsPages];
}
