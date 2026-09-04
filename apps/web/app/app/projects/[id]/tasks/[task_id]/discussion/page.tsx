import { TaskDiscussionClient } from "@/components/TaskDiscussionClient";
import { requireCurrentUser } from "@/lib/auth/server";

export default async function ProjectTaskDiscussionPage({ params }: { params: { id: string; task_id: string } }) {
  const currentUser = await requireCurrentUser(`/app/projects/${params.id}/tasks/${params.task_id}/discussion`);
  return <TaskDiscussionClient taskId={decodeURIComponent(params.task_id)} routeProjectId={decodeURIComponent(params.id)} currentUser={currentUser} />;
}
