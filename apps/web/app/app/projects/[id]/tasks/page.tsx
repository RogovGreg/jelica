import { ProjectTasksClient } from "@/components/ProjectTasksClient";
import { requireCurrentUser } from "@/lib/auth/server";

export default async function ProjectTasksPage({ params }: { params: { id: string } }) {
  await requireCurrentUser(`/app/projects/${params.id}/tasks`);
  return <ProjectTasksClient projectId={decodeURIComponent(params.id)} />;
}
