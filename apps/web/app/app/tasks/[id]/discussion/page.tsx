import { TaskDiscussionClient } from "@/components/TaskDiscussionClient";
import { requireCurrentUser } from "@/lib/auth/server";

export default async function TaskDiscussionPage({ params }: { params: { id: string } }) {
  const currentUser = await requireCurrentUser(`/app/tasks/${params.id}/discussion`);
  return <TaskDiscussionClient taskId={decodeURIComponent(params.id)} currentUser={currentUser} />;
}
