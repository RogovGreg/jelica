import { ProjectsClient } from "@/components/ProjectsClient";
import { requireCurrentUser } from "@/lib/auth/server";

export default async function ProjectsPage() {
  await requireCurrentUser("/app/projects");
  return <ProjectsClient />;
}
