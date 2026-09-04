import { ProjectMembersClient } from "@/components/ProjectMembersClient";
import { requireCurrentUser } from "@/lib/auth/server";

type MembersPageProps = { params: { id: string } };

export default async function ProjectMembersPage({ params }: MembersPageProps) {
  const currentUser = await requireCurrentUser(`/app/projects/${params.id}/members`);
  return <ProjectMembersClient projectId={decodeURIComponent(params.id)} currentUser={currentUser} />;
}
