import type { Metadata } from "next";

export const SITE_NAME = "JELICA";
export const SITE_URL = "https://jelica.bio";
export const SITE_DESCRIPTION =
  "JELICA is a platform for comparative genomic analysis, combining sequence validation, alignment, genetic distances, phylogenetic analysis, and reproducible results.";
export const HOME_TITLE = "JELICA — Comparative Genomics Analysis";
export const SOCIAL_IMAGE_PATH = "/social-preview.png";

export function siteUrl(pathname: string): string {
  return new URL(pathname, SITE_URL).toString();
}

export function publicPageMetadata(title: string, description: string, pathname: string): Metadata {
  const fullTitle = `${title} | ${SITE_NAME}`;
  return {
    title,
    description,
    alternates: { canonical: pathname },
    openGraph: {
      title: fullTitle,
      description,
      type: "website",
      siteName: SITE_NAME,
      url: siteUrl(pathname),
      images: [{ url: SOCIAL_IMAGE_PATH, width: 1200, height: 630, alt: HOME_TITLE }],
    },
    twitter: {
      card: "summary_large_image",
      title: fullTitle,
      description,
      images: [SOCIAL_IMAGE_PATH],
    },
  };
}
