import Link from "next/link";

import { CreateProjectForm } from "@/components/CreateProjectForm";
import { requireCurrentUser } from "@/lib/auth/server";
import { DEFAULT_LOCALE, translate } from "@/lib/i18n";

export default async function NewProjectPage() {
  await requireCurrentUser("/app/projects/new");
  return <section className="panel stack"><div><h1 style={{ margin: 0 }}>{translate(DEFAULT_LOCALE, "project.action.create")}</h1><p className="muted">{translate(DEFAULT_LOCALE, "project.page.subtitle")}</p></div><CreateProjectForm /><Link href="/app/projects" className="secondary-button">{translate(DEFAULT_LOCALE, "project.overview.back")}</Link></section>;
}
