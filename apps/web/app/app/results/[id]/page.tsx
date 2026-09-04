import Link from "next/link";

import { ErrorState } from "@/components/ErrorState";
import { RestrictedResourceState } from "@/components/RestrictedResourceState";
import { ResultCard } from "@/components/ResultCard";
import { TranslatedText } from "@/components/TranslatedText";
import { getTaskResult } from "@/lib/api/client";
import { isResourceUnavailableError } from "@/lib/api/errors";

type AppResultDetailsPageProps = {
  params: {
    id: string;
  };
};

export default async function AppResultDetailsPage({ params }: AppResultDetailsPageProps) {
  const taskId = decodeURIComponent(params.id);
  try {
    const result = await getTaskResult(taskId);
    return (
      <section className="panel stack">
        <div>
          <h1 style={{ margin: 0 }}><TranslatedText id="task.label.task-prefix" values={{ task: taskId }} /></h1>
          <p className="muted" style={{ marginTop: "0.35rem" }}>
            <TranslatedText id="result.page.api-description" values={{ task_id: taskId }} />
          </p>
        </div>
        <ResultCard result={result} />
        <div className="actions-row">
          <Link href={`/app/tasks/${encodeURIComponent(taskId)}`} className="secondary-button">
            <TranslatedText id="common.action.back-to-task-details" />
          </Link>
          <Link href="/app/results" className="secondary-button">
            <TranslatedText id="common.action.back-to-results" />
          </Link>
          <Link href="/app/tasks/new" className="secondary-button">
            <TranslatedText id="common.action.new-task" />
          </Link>
        </div>
      </section>
    );
  } catch (error) {
    if (isResourceUnavailableError(error)) {
      return <RestrictedResourceState variant="resource-unavailable" resourceType="result" />;
    }
    return (
      <ErrorState
        title={<TranslatedText id="result.page.data-unavailable" />}
        description={<TranslatedText id="result.page.data-load-failed" values={{ task: taskId }} />}
      >
        <div className="actions-row">
          <Link href={`/app/tasks/${encodeURIComponent(taskId)}`} className="secondary-button">
            <TranslatedText id="common.action.back-to-task-details" />
          </Link>
          <Link href="/app/results" className="secondary-button">
            <TranslatedText id="common.action.back-to-results" />
          </Link>
        </div>
      </ErrorState>
    );
  }
}
