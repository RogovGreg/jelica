import Link from "next/link";

import { AnalysisServiceUnavailable } from "@/components/AnalysisServiceUnavailable";
import { EmptyState } from "@/components/EmptyState";
import { TaskListView } from "@/components/TaskListView";
import { TaskListFilters } from "@/components/TaskListFilters";
import { TaskListRefreshButton } from "@/components/TaskListRefreshButton";
import { TranslatedText } from "@/components/TranslatedText";
import { getTaskList } from "@/lib/api/client";
import { toErrorMessage } from "@/lib/api/errors";
import type { TaskListItem } from "@/types/api";

type SearchParams = Record<string, string | string[] | undefined>;
const VALID_TASK_STATES = new Set(["created", "queued", "running", "waiting", "pausing", "paused", "resuming", "completed", "failed", "interrupted", "cancelled"]);

export default async function AppTasksPage({ searchParams }: Readonly<{ searchParams?: SearchParams }>) {
  const owner = firstValue(searchParams?.owner) === "me" ? "me" : undefined;
  const project = firstValue(searchParams?.project) === "none" ? "none" : undefined;
  const projectIds = values(searchParams?.project_id);
  const requestedState = firstValue(searchParams?.state)?.trim().toLowerCase();
  const state = requestedState && VALID_TASK_STATES.has(requestedState) ? requestedState : undefined;
  let tasks: TaskListItem[];
  try {
    const response = await getTaskList({ owner, project, project_id: projectIds.length ? projectIds : undefined, state: state ? [state] : undefined });
    tasks = response.items;
  } catch (error) {
    return (
      <AnalysisServiceUnavailable
        title={<TranslatedText id="task.list.unavailable" />}
        description={toErrorMessage(error)}
        fallbackLinks={[
          { href: "/app/tasks/new", label: <TranslatedText id="common.action.create-task" /> },
          { href: "/app/support", label: <TranslatedText id="nav.support" /> },
        ]}
      />
    );
  }

  if (tasks.length === 0) {
    return (
      <EmptyState
        title={<TranslatedText id="task.list.empty" />}
        description={<TranslatedText id="task.list.empty-description" />}
      >
        <p className="muted" style={{ margin: 0 }}><TranslatedText id="task.list.latest-known" /></p>
        <TaskListFilters initialOwner={owner} initialProject={project} initialProjectId={projectIds[0]} initialState={state} />
        <div className="actions-row">
          <Link href="/app/tasks/new" className="primary-button">
            <TranslatedText id="task.list.create" />
          </Link>
          <TaskListRefreshButton />
        </div>
      </EmptyState>
    );
  }

  return (
    <section className="panel stack">
      <div>
        <h1 style={{ margin: 0 }}>
          <TranslatedText id="page.tasks.title" />
        </h1>
        <p className="muted" style={{ marginTop: "0.35rem" }}>
          <TranslatedText id="task.list.latest-known" />
        </p>
        <div className="actions-row" style={{ marginTop: "0.8rem" }}>
          <Link href="/app/tasks/new" className="primary-button">
            <TranslatedText id="common.action.new-task" />
          </Link>
          <TaskListRefreshButton />
        </div>
      </div>

      <TaskListFilters initialOwner={owner} initialProject={project} initialProjectId={projectIds[0]} initialState={state} />

      <TaskListView tasks={tasks} />
    </section>
  );
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function values(value: string | string[] | undefined): string[] {
  return (Array.isArray(value) ? value : value ? [value] : []).filter((item) => item.trim() !== "");
}
