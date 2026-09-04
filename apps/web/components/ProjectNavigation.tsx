import Link from "next/link";

import { useI18n } from "@/components/I18nProvider";

export function ProjectNavigation({ projectId, active }: Readonly<{ projectId: string; active: "overview" | "tasks" | "members" | "discussion" }>) {
  const { t } = useI18n();
  const base = `/app/projects/${encodeURIComponent(projectId)}`;
  return <nav className="project-navigation" aria-label={t("project.navigation.label")}>
    <Link className={active === "overview" ? "active" : undefined} href={base}>{t("project.navigation.overview")}</Link>
    <Link className={active === "tasks" ? "active" : undefined} href={`${base}/tasks`}>{t("project.navigation.tasks")}</Link>
    <Link className={active === "members" ? "active" : undefined} href={`${base}/members`}>{t("project.navigation.members")}</Link>
    <Link className={active === "discussion" ? "active" : undefined} href={`${base}/discussion`}>{t("project.navigation.discussion")}</Link>
  </nav>;
}
