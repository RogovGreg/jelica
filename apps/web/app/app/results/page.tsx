import Link from "next/link";

import { AnalysisServiceUnavailable } from "@/components/AnalysisServiceUnavailable";
import { EmptyState } from "@/components/EmptyState";
import { TaskStatusBadge } from "@/components/TaskStatusBadge";
import { TranslatedText } from "@/components/TranslatedText";
import { getTaskList } from "@/lib/api/client";
import { toErrorMessage } from "@/lib/api/errors";
import { getTaskCanonicalPath } from "@/lib/tasks/routing";
import { normalizeTaskState, type TaskListItem } from "@/types/api";

export default async function AppResultsPage() {
  let completedTasks: TaskListItem[];
  try {
    const response = await getTaskList();
    completedTasks = response.items.filter((item) => normalizeTaskState(item.state) === "completed");
  } catch (error) {
    return (
      <AnalysisServiceUnavailable
        title={<TranslatedText id="result.page.data-unavailable" />}
        description={toErrorMessage(error)}
        fallbackLinks={[
          { href: "/app/tasks", label: <TranslatedText id="common.action.open-tasks" /> },
          { href: "/app/support", label: <TranslatedText id="nav.support" /> },
        ]}
      />
    );
  }

  if (completedTasks.length === 0) {
    return (
      <EmptyState
        title={<TranslatedText id="result.page.no-results" />}
        description={<TranslatedText id="result.page.no-results-description" />}
      >
        <div className="actions-row">
          <Link href="/app/tasks" className="secondary-button">
            <TranslatedText id="common.action.open-tasks" />
          </Link>
          <Link href="/app/tasks/new" className="primary-button">
            <TranslatedText id="common.action.create-task" />
          </Link>
        </div>
      </EmptyState>
    );
  }

  return (
    <section className="panel stack">
      <div>
        <h1 style={{ margin: 0 }}><TranslatedText id="result.page.title" /></h1>
        <p className="muted" style={{ marginTop: "0.35rem" }}>
          <TranslatedText id="task.results.latest-known" />
        </p>
      </div>

      <table className="task-table" aria-label="Completed task results">
        <thead>
          <tr>
            <th><TranslatedText id="result.page.task-id" /></th>
            <th><TranslatedText id="result.page.state" /></th>
            <th><TranslatedText id="result.page.updated" /></th>
            <th><TranslatedText id="result.page.actions" /></th>
          </tr>
        </thead>
        <tbody>
          {completedTasks.map((task) => (
            <tr key={task.task_id}>
              <td>
                <code>{task.task_id}</code>
              </td>
              <td>
                <TaskStatusBadge status={task.state} />
              </td>
              <td>{formatDateTime(task.updated_at)}</td>
              <td>
                <div className="actions-row">
                  <Link href={`/app/results/${encodeURIComponent(task.task_id)}`} className="primary-button">
                    <TranslatedText id="common.action.open-result" />
                  </Link>
                  <Link href={getTaskCanonicalPath(task)} className="secondary-button">
                    <TranslatedText id="common.action.open-task" />
                  </Link>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function formatDateTime(rawValue: string): string {
  const value = new Date(rawValue);
  if (Number.isNaN(value.getTime())) {
    return rawValue;
  }
  return value.toLocaleString();
}
