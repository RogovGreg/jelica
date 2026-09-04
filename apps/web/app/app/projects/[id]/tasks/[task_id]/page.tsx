import { TaskDetailsClient } from "@/components/TaskDetailsClient";
import { requireCurrentUser } from "@/lib/auth/server";

export default async function ProjectTaskDetailsPage({ params }: { params: { id: string; task_id: string } }) {
  await requireCurrentUser(`/app/projects/${params.id}/tasks/${params.task_id}`);
  return <TaskDetailsClient taskId={decodeURIComponent(params.task_id)} routeProjectId={decodeURIComponent(params.id)} />;
}
