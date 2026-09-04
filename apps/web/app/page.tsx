import Link from "next/link";

import { LandingAnchorNav } from "@/components/LandingAnchorNav";
import { TranslatedText } from "@/components/TranslatedText";
import { listLatestNews } from "@/lib/content/news";
import type { Metadata } from "next";

export const metadata: Metadata = {
  alternates: { canonical: "/" },
};

const latestNews = listLatestNews(3);
const SHOW_LANDING_SECTIONS = false;

export default function HomePage() {
  return (
    <div className="landing-root">
      <section id="hero" className="landing-section panel stack">
        <p className="landing-kicker">JELICA</p>
        <h1 style={{ margin: 0 }}><TranslatedText id="landing.hero.title" /></h1>
        <p className="muted" style={{ margin: 0 }}>
          <TranslatedText id="landing.hero.description" />
        </p>
        <div className="actions-row">
          <Link href="/download" className="primary-button">
            <TranslatedText id="nav.download" />
          </Link>
          <Link href="/app/tasks" className="secondary-button">
            <TranslatedText id="nav.run-online" />
          </Link>
          <Link href="/docs" className="secondary-button">
            <TranslatedText id="nav.documentation" />
          </Link>
        </div>
        <div className="landing-news-block stack">
          <h2 style={{ margin: 0 }}><TranslatedText id="landing.news.latest" /></h2>
          <ul className="landing-news-list">
            {latestNews.map((item) => (
              <li key={item.slug}>
                <Link href={`/news/${item.slug}`}>{item.title}</Link>
                <span className="muted">{item.date}</span>
              </li>
            ))}
          </ul>
          <Link href="/news" className="secondary-button">
            <TranslatedText id="landing.news.all" />
          </Link>
        </div>
        {SHOW_LANDING_SECTIONS ? <LandingAnchorNav /> : null}
      </section>

      {SHOW_LANDING_SECTIONS ? <>
      <section id="statistics" className="landing-section panel stack">
        <h2 style={{ margin: 0 }}>
          <TranslatedText id="report.section.statistics" />
        </h2>
        <div className="landing-grid">
          <article className="state-box">
            <strong><TranslatedText id="landing.statistics.genomes" /></strong>
            <p className="muted" style={{ marginBottom: 0 }}><TranslatedText id="landing.statistics.genomes-description" /></p>
          </article>
          <article className="state-box">
            <strong><TranslatedText id="landing.statistics.formats" /></strong>
            <p className="muted" style={{ marginBottom: 0 }}><TranslatedText id="landing.statistics.formats-description" /></p>
          </article>
          <article className="state-box">
            <strong><TranslatedText id="landing.statistics.phases" /></strong>
            <p className="muted" style={{ marginBottom: 0 }}>
              <TranslatedText id="landing.statistics.phases-description" />
            </p>
          </article>
        </div>
      </section>

      <section id="comparative" className="landing-section panel stack">
        <h2 style={{ margin: 0 }}><TranslatedText id="landing.comparative.title" /></h2>
        <p className="muted" style={{ margin: 0 }}>
          <TranslatedText id="landing.comparative.description" />
        </p>
        <div className="state-box">
          <TranslatedText id="landing.comparative.placeholder" />
        </div>
      </section>

      <section id="phylogenetics" className="landing-section panel stack">
        <h2 style={{ margin: 0 }}><TranslatedText id="landing.phylogenetics.title" /></h2>
        <p className="muted" style={{ margin: 0 }}>
          <TranslatedText id="landing.phylogenetics.description" />
        </p>
        <div className="state-box">
          <TranslatedText id="landing.phylogenetics.placeholder" />
        </div>
      </section>

      <section id="reproducibility" className="landing-section panel stack">
        <h2 style={{ margin: 0 }}><TranslatedText id="landing.reproducibility.title" /></h2>
        <div className="landing-grid">
          <article className="state-box">
            <strong><TranslatedText id="landing.reproducibility.packages" /></strong>
            <p className="muted" style={{ marginBottom: 0 }}><TranslatedText id="landing.reproducibility.packages-description" /></p>
          </article>
          <article className="state-box">
            <strong><TranslatedText id="landing.reproducibility.tasks" /></strong>
            <p className="muted" style={{ marginBottom: 0 }}><TranslatedText id="landing.reproducibility.tasks-description" /></p>
          </article>
          <article className="state-box">
            <strong><TranslatedText id="landing.reproducibility.partial" /></strong>
            <p className="muted" style={{ marginBottom: 0 }}>
              <TranslatedText id="landing.reproducibility.partial-description" />
            </p>
          </article>
        </div>
      </section>

      <section id="platforms" className="landing-section panel stack">
        <h2 style={{ margin: 0 }}><TranslatedText id="landing.platforms.title" /></h2>
        <div className="landing-grid">
          <article className="state-box">
            <strong><TranslatedText id="landing.platforms.cli" /></strong>
            <p className="muted" style={{ marginBottom: 0 }}><TranslatedText id="landing.platforms.cli-description" /></p>
          </article>
          <article className="state-box">
            <strong><TranslatedText id="landing.platforms.desktop" /></strong>
            <p className="muted" style={{ marginBottom: 0 }}><TranslatedText id="landing.platforms.desktop-description" /></p>
          </article>
          <article className="state-box">
            <strong><TranslatedText id="landing.platforms.web" /></strong>
            <p className="muted" style={{ marginBottom: 0 }}><TranslatedText id="landing.platforms.web-description" /></p>
          </article>
        </div>
      </section>

      <section id="technical" className="landing-section panel stack">
        <h2 style={{ margin: 0 }}><TranslatedText id="landing.technical.title" /></h2>
        <p className="muted" style={{ margin: 0 }}>
          <TranslatedText id="landing.technical.description" />
        </p>
        <div className="actions-row">
          <Link href="/about" className="secondary-button">
            <TranslatedText id="nav.about" />
          </Link>
          <Link href="/license" className="secondary-button">
            <TranslatedText id="public.legal.license-title" />
          </Link>
          <Link href="/terms" className="secondary-button">
            <TranslatedText id="public.legal.terms-title" />
          </Link>
          <Link href="/privacy" className="secondary-button">
            <TranslatedText id="public.legal.privacy-title" />
          </Link>
        </div>
      </section>
      </> : null}
    </div>
  );
}
