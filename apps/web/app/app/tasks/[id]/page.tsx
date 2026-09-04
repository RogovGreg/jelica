import { TaskDetailsClient } from "@/components/TaskDetailsClient";

type AppTaskDetailsPageProps = {
  params: {
    id: string;
  };
};

export default function AppTaskDetailsPage({ params }: AppTaskDetailsPageProps) {
  const taskId = decodeURIComponent(params.id);
  return <TaskDetailsClient taskId={taskId} />;
}
