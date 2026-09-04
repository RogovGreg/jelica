import { ProjectDiscussionClient } from "@/components/ProjectDiscussionClient";
import { requireCurrentUser } from "@/lib/auth/server";

export default async function ProjectDiscussionPage({ params }: { params: { id: string } }) {
  const currentUser = await requireCurrentUser(`/app/projects/${params.id}/discussion`);
  return (
    <ProjectDiscussionClient
      projectId={decodeURIComponent(params.id)}
      currentUser={currentUser}
    />
  );
}
