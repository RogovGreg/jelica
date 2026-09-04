import type { TaskListItem } from "@/types/api";

type TaskWithProject = Pick<TaskListItem, "task_id" | "project_id">;

export function getTaskCanonicalPath(task: TaskWithProject): string {
  const taskId = encodeURIComponent(task.task_id);
  return task.project_id
    ? `/app/projects/${encodeURIComponent(task.project_id)}/tasks/${taskId}`
    : `/app/tasks/${taskId}`;
}

export function getTaskDiscussionCanonicalPath(task: TaskWithProject): string {
  const taskId = encodeURIComponent(task.task_id);
  return task.project_id
    ? `/app/projects/${encodeURIComponent(task.project_id)}/tasks/${taskId}/discussion`
    : `/app/tasks/${taskId}/discussion`;
}
