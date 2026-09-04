import { ProjectOverviewClient } from "@/components/ProjectOverviewClient";
import { requireCurrentUser } from "@/lib/auth/server";

type ProjectPageProps = { params: { id: string } };

export default async function ProjectPage({ params }: ProjectPageProps) {
  const currentUser = await requireCurrentUser(`/app/projects/${params.id}`);
  return <ProjectOverviewClient projectId={decodeURIComponent(params.id)} currentUser={currentUser} />;
}
