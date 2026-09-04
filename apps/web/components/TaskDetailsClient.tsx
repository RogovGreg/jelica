"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { AnalysisServiceUnavailable } from "@/components/AnalysisServiceUnavailable";
import { Breadcrumbs } from "@/components/Breadcrumbs";
import { ErrorState } from "@/components/ErrorState";
import { LoadingState } from "@/components/LoadingState";
import { ProjectNavigation } from "@/components/ProjectNavigation";
import { RestrictedResourceState } from "@/components/RestrictedResourceState";
import { ResultCard } from "@/components/ResultCard";
import { TaskStatusBadge } from "@/components/TaskStatusBadge";
import { TaskDiscussionClient } from "@/components/TaskDiscussionClient";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { attachTaskToProject, detachTaskFromProject, getCurrentUser, getProject, getProjects, getTaskDiscussionComments, getTaskList, getTaskResult, getTaskStatus, pauseTask, resumeTask, startTask } from "@/lib/api/client";
import { isResourceUnavailableError, toApiClientError, toLocalizedErrorMessage } from "@/lib/api/errors";
import { getTaskCanonicalPath, getTaskDiscussionCanonicalPath } from "@/lib/tasks/routing";
import { useI18n } from "@/components/I18nProvider";
import { isTerminalTaskState, normalizeTaskState, type Project, type TaskListItem, type TaskResultLookupResponse, type TaskStatusSnapshot } from "@/types/api";

const POLL_INTERVAL_SECONDS = 5;
const POLL_INTERVAL_MS = POLL_INTERVAL_SECONDS * 1000;

type TaskDetailsClientProps = {
  taskId: string;
  routeProjectId?: string;
};

export function TaskDetailsClient({ taskId, routeProjectId }: TaskDetailsClientProps) {
  const router = useRouter();
  const { t } = useI18n();
  const [taskStatus, setTaskStatus] = useState<TaskStatusSnapshot | null>(null);
  const [taskResult, setTaskResult] = useState<TaskResultLookupResponse | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [resultError, setResultError] = useState<string | null>(null);
  const [isInitialLoading, setIsInitialLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<Date | null>(null);
  const [currentUser, setCurrentUser] = useState<import("@/types/api").AuthUser | null>(null);
  const currentUserId = currentUser?.id ?? null;
  const [taskListItem, setTaskListItem] = useState<TaskListItem | null>(null);
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [projectError, setProjectError] = useState<string | null>(null);
  const [restrictedVariant, setRestrictedVariant] = useState<"resource-unavailable" | "access-denied" | null>(null);
  const [lifecyclePending, setLifecyclePending] = useState<"start" | "pause" | "resume" | null>(null);
  const [lifecycleError, setLifecycleError] = useState<string | null>(null);
  const hadLoadedTask = useRef(false);
  const statusRequestId = useRef(0);
  const lifecycleRequestId = useRef(0);

  useEffect(() => {
    let cancelled = false;
    void getCurrentUser().then(async (user) => {
      if (cancelled) return;
      setCurrentUser(user);
      const [taskResponse, projectResponse] = await Promise.allSettled([getTaskList(), getProjects()]);
      if (cancelled) return;
      if (taskResponse.status === "fulfilled") setTaskListItem(taskResponse.value.items.find((item) => item.task_id === taskId) ?? null);
      if (projectResponse.status === "fulfilled") setProjects(projectResponse.value.items);
    }).catch(() => { if (!cancelled) setCurrentUser(null); });
    if (routeProjectId) {
      void       getProject(routeProjectId).then((loaded) => { if (!cancelled) setProject(loaded); }).catch((error) => { if (!cancelled && isResourceUnavailableError(error)) setRestrictedVariant("resource-unavailable"); else if (!cancelled) setProjectError(toLocalizedErrorMessage(error, t)); });
    }
    return () => { cancelled = true; };
  }, [routeProjectId, t, taskId]);

  const refreshTask = useCallback(async (): Promise<TaskStatusSnapshot> => {
    const requestId = ++statusRequestId.current;
    const nextStatus = await getTaskStatus(taskId);
    if (requestId !== statusRequestId.current) return nextStatus;
    hadLoadedTask.current = true;
    setTaskStatus(nextStatus);
    setStatusError(null);
    setLastUpdatedAt(new Date());

    if (normalizeTaskState(nextStatus.state) === "completed") {
      try {
        const nextResult = await getTaskResult(taskId);
        if (requestId !== statusRequestId.current) return nextStatus;
        setTaskResult(nextResult);
        setResultError(null);
      } catch (error) {
        if (requestId !== statusRequestId.current) return nextStatus;
        setTaskResult(null);
        setResultError(toLocalizedErrorMessage(error, t));
      }
    } else {
      setTaskResult(null);
      setResultError(null);
    }

    return nextStatus;
  }, [t, taskId]);

  useEffect(() => {
    hadLoadedTask.current = false;
    statusRequestId.current += 1;
    setRestrictedVariant(null);
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;

    const poll = async () => {
      if (cancelled) {
        return;
      }

      setIsRefreshing(true);
      try {
        const nextStatus = await refreshTask();
        if (!cancelled && !isTerminalTaskState(nextStatus.state)) {
          timeoutId = setTimeout(() => {
            void poll();
          }, POLL_INTERVAL_MS);
        }
      } catch (error) {
        if (!cancelled) {
          if (isResourceUnavailableError(error)) setRestrictedVariant(hadLoadedTask.current ? "access-denied" : "resource-unavailable");
          setStatusError(toLocalizedErrorMessage(error, t));
          timeoutId = setTimeout(() => {
            void poll();
          }, POLL_INTERVAL_MS);
        }
      } finally {
        if (!cancelled) {
          setIsInitialLoading(false);
          setIsRefreshing(false);
        }
      }
    };

    void poll();

    return () => {
      cancelled = true;
      statusRequestId.current += 1;
      lifecycleRequestId.current += 1;
      if (timeoutId) {
        clearTimeout(timeoutId);
      }
    };
  }, [refreshTask, t]);

  useEffect(() => {
    if (!taskStatus) return;
    const canonicalPath = getTaskCanonicalPath({ task_id: taskStatus.task_id, project_id: taskStatus.project_id });
    const isNested = Boolean(routeProjectId);
    if ((isNested && taskStatus.project_id !== routeProjectId) || (!isNested && taskStatus.project_id)) {
      router.replace(canonicalPath);
    }
  }, [routeProjectId, router, taskStatus]);

  const refreshNow = useCallback(async () => {
    setIsRefreshing(true);
    try {
      await refreshTask();
    } catch (error) {
      if (isResourceUnavailableError(error)) setRestrictedVariant(hadLoadedTask.current ? "access-denied" : "resource-unavailable");
      setStatusError(toLocalizedErrorMessage(error, t));
    } finally {
      setIsInitialLoading(false);
      setIsRefreshing(false);
    }
  }, [refreshTask, t]);

  const performLifecycle = useCallback(async (action: "start" | "pause" | "resume") => {
    const requestId = ++lifecycleRequestId.current;
    setLifecyclePending(action); setLifecycleError(null);
    try {
      const next = action === "start" ? await startTask(taskId) : action === "pause" ? await pauseTask(taskId) : await resumeTask(taskId);
      if (requestId !== lifecycleRequestId.current) return;
      setTaskStatus(next); setStatusError(null); setLastUpdatedAt(new Date());
    } catch (error) {
      if (requestId !== lifecycleRequestId.current) return;
      const apiError = toApiClientError(error);
      setLifecycleError(toLocalizedErrorMessage(error, t));
      if (apiError.status === 409 || apiError.status === 403) void refreshNow();
    } finally { if (requestId === lifecycleRequestId.current) setLifecyclePending(null); }
  }, [refreshNow, t, taskId]);

  if (isInitialLoading && taskStatus === null && statusError === null) {
    return (
      <LoadingState
        title={t("task.loading.title")}
        description={t("task.loading.description")}
      />
    );
  }

  if (restrictedVariant) return <RestrictedResourceState variant={restrictedVariant} resourceType="task" />;

  if (taskStatus === null) {
    return (
      <AnalysisServiceUnavailable
        title={t("task.status.unavailable")}
        description={statusError ?? t("common.error.generic")}
        onRetry={() => void refreshNow()}
        retryLabel={t("common.action.retry-now")}
      />
    );
  }

  const terminal = isTerminalTaskState(taskStatus.state);
  const resultLink = `/app/results/${encodeURIComponent(taskStatus.task_id)}`;
  const redirecting = (routeProjectId && taskStatus.project_id !== routeProjectId) || (!routeProjectId && Boolean(taskStatus.project_id));
  if (redirecting) return <LoadingState title={t("task.routing.redirecting")} />;

  return (
    <section className="panel stack">
      {routeProjectId && project ? <><Breadcrumbs items={[{ label: t("breadcrumbs.projects"), href: "/app/projects" }, { label: project.name, href: `/app/projects/${encodeURIComponent(routeProjectId)}` }, { label: t("project.navigation.tasks"), href: `/app/projects/${encodeURIComponent(routeProjectId)}/tasks` }, { label: t("task.label.task-prefix", { task: taskStatus.task_id }) }]} label={t("breadcrumbs.label")} /><ProjectNavigation projectId={routeProjectId} active="tasks" /></> : <Breadcrumbs items={[{ label: t("breadcrumbs.tasks"), href: "/app/tasks" }, { label: t("task.label.task-prefix", { task: taskStatus.task_id }) }]} label={t("breadcrumbs.label")} />}
      {routeProjectId && project?.status === "frozen" ? <div className="state-box state-warning" role="status">{t("project.status.banner")}</div> : null}
      {projectError ? <div className="state-box state-warning" role="status">{projectError}</div> : null}
      <div className="stack" style={{ gap: "0.45rem" }}>
        <h1 style={{ margin: 0 }}>{t("task.label.task-prefix", { task: taskStatus.task_id })}</h1>
        <p className="muted" style={{ margin: 0 }}>
          {t("task.detail.trace-id", { trace: taskStatus.trace_id ?? "—" })}
        </p>
      </div>

      <div className="stack status-grid">
        <div>
          <strong>{t("task.detail.status")}</strong> <TaskStatusBadge status={taskStatus.state} />
        </div>
        <div>
          <strong>{t("task.detail.current-stage")}</strong> {taskStatus.current_stage ?? "—"}
        </div>
        <div>
          <strong>{t("task.detail.active-job-state")}</strong> {taskStatus.active_job_state ?? "—"}
        </div>
        <div><strong>{t("task.detail.progress")}</strong> {taskStatus.progress === null ? "—" : `${taskStatus.progress}%`} {taskStatus.progress !== null ? <progress max={100} value={taskStatus.progress} aria-label={t("task.detail.progress-aria", { progress: taskStatus.progress })} /> : null}</div>
        <div>
          <strong>{t("task.detail.source")}</strong> {taskStatus.state_source}
        </div>
        <div>
          <strong>{t("task.detail.authoritative")}</strong> {taskStatus.authoritative ? t("task.detail.authoritative-yes") : t("task.detail.authoritative-no")}
        </div>
        <div>
          <strong>{t("task.detail.command-id")}</strong> {taskStatus.command_id ?? "—"}
        </div>
        <div>
          <strong>{t("task.detail.projection-updated")}</strong>{" "}
          {taskStatus.projection_updated_at ?? "—"}
        </div>
      </div>

      {taskStatus.stale_state ? (
        <div className="state-box state-warning">
          {t("task.detail.stale-warning")}
        </div>
      ) : null}
      {taskStatus.detail ? <div className="state-box">{taskStatus.detail}</div> : null}
      {taskStatus.state === "failed" && taskStatus.detail ? <div className="state-box state-error" role="alert">{taskStatus.detail}</div> : null}

      {taskStatus.can_control_lifecycle ? <section className="panel stack" aria-labelledby="lifecycle-heading"><h2 id="lifecycle-heading">{t("task.lifecycle.title")}</h2><div className="actions-row"><button type="button" className="secondary-button" disabled={Boolean(lifecyclePending)} onClick={() => void performLifecycle("start")}>{lifecyclePending === "start" ? t("task.lifecycle.starting") : t("task.lifecycle.start")}</button><button type="button" className="secondary-button" disabled={Boolean(lifecyclePending)} onClick={() => void performLifecycle("pause")}>{lifecyclePending === "pause" ? t("task.lifecycle.pausing") : t("task.lifecycle.pause")}</button><button type="button" className="secondary-button" disabled={Boolean(lifecyclePending)} onClick={() => void performLifecycle("resume")}>{lifecyclePending === "resume" ? t("task.lifecycle.resuming") : t("task.lifecycle.resume")}</button></div>{lifecycleError ? <div className="state-box state-error" role="alert">{lifecycleError}</div> : null}</section> : null}

      {currentUserId && taskListItem?.owner_user_id === currentUserId ? <ProjectAssignmentControl taskId={taskId} taskProjectId={taskStatus.project_id} projects={projects} onNavigate={(path) => router.replace(path)} /> : null}

      <p className="muted" style={{ margin: 0 }}>
        {terminal
          ? t("task.polling.terminal")
          : t("task.polling.active", { seconds: POLL_INTERVAL_SECONDS })}
        {lastUpdatedAt ? t("task.polling.last-update", { time: lastUpdatedAt.toLocaleTimeString() }) : ""}
        {isRefreshing ? t("task.polling.refreshing") : ""}
      </p>

      <div className="actions-row">
        <button type="button" className="secondary-button" onClick={() => void refreshNow()} disabled={isRefreshing}>
          {t("common.action.refresh-now")}
        </button>
        <Link href="/app/tasks/new" className="secondary-button">
          {t("common.action.new-task")}
        </Link>
      </div>

      {normalizeTaskState(taskStatus.state) === "completed" ? (
        <>
          {taskResult ? <ResultCard result={taskResult} /> : null}
          {resultError ? (
            <ErrorState title={t("result.lookup.unavailable")} description={resultError}>
              <div className="actions-row">
                <button type="button" className="secondary-button" onClick={() => void refreshNow()}>
                  {t("common.action.retry-result-lookup")}
                </button>
              </div>
            </ErrorState>
          ) : null}
          {taskResult?.available ? (
            <div className="actions-row">
              <Link href={resultLink} className="primary-button">
                {t("result.action.open-page")}
              </Link>
            </div>
          ) : null}
        </>
      ) : null}
      {currentUser ? <TaskDiscussionClient taskId={taskId} currentUser={currentUser} compact routeProjectId={routeProjectId} /> : null}
      {currentUser ? <div className="actions-row"><Link href={getTaskDiscussionCanonicalPath({ task_id: taskId, project_id: taskStatus.project_id })} className="secondary-button">{t("task.discussion.open")}</Link></div> : null}
    </section>
  );
}

function ProjectAssignmentControl({ taskId, taskProjectId, projects, onNavigate }: Readonly<{ taskId: string; taskProjectId: string | null; projects: Project[] | null; onNavigate: (path: string) => void }>) {
  const { t } = useI18n();
  const [selectedProjectId, setSelectedProjectId] = useState(taskProjectId ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<string | null>(null);
  useEffect(() => setSelectedProjectId(taskProjectId ?? ""), [taskProjectId]);
  const eligible = (projects ?? []).filter((item) => item.status === "active" && ["member", "supervisor"].includes(item.current_user_role ?? ""));
  const currentProject = (projects ?? []).find((item) => item.id === taskProjectId);
  const options = currentProject && !eligible.some((item) => item.id === currentProject.id) ? [currentProject, ...eligible] : eligible;
  async function performApply() {
    if (selectedProjectId === (taskProjectId ?? "")) return;
    setBusy(true); setError(null);
    try {
      if (selectedProjectId) {
        await attachTaskToProject(selectedProjectId, taskId);
      } else if (taskProjectId) {
        await detachTaskFromProject(taskProjectId, taskId);
      }
      onNavigate(getTaskCanonicalPath({ task_id: taskId, project_id: selectedProjectId || null }));
    } catch (requestError) {
      setError(toLocalizedErrorMessage(requestError, t));
    } finally { setBusy(false); }
  }
  async function apply() {
    if (!selectedProjectId || selectedProjectId === (taskProjectId ?? "")) { await performApply(); return; }
    try {
      const history = await getTaskDiscussionComments(taskId);
      if (history.items.length > 0) { setConfirmTarget(selectedProjectId); return; }
    } catch { /* unavailable discussion means first-ever attach */ }
    await performApply();
  }
  return <><section className="panel stack project-assignment"><h2>{t("task.project.assignment")}</h2><label className="input-field"><span>{t("task.project.label")}</span><select value={selectedProjectId} onChange={(event) => setSelectedProjectId(event.target.value)} disabled={busy || projects === null}><option value="">{t("task.project.none")}</option>{options.map((item) => <option key={item.id} value={item.id}>{item.name}{item.status === "frozen" ? ` (${t("project.status.frozen")})` : ""}</option>)}</select></label>{projects === null ? <span className="muted">{t("task.project.loading")}</span> : null}{error ? <div className="state-box state-error" role="alert">{error}</div> : null}<button type="button" className="primary-button" onClick={() => void apply()} disabled={busy || selectedProjectId === (taskProjectId ?? "")}>{busy ? t("task.project.applying") : t("task.project.apply")}</button></section>{confirmTarget ? <ConfirmDialog title={t(taskProjectId ? "task.discussion.reattach.title" : "task.discussion.attach.title")} description={t(taskProjectId ? "task.discussion.reattach.description" : "task.discussion.attach.description")} actionLabel={t("task.project.apply")} busy={busy} onCancel={() => setConfirmTarget(null)} onConfirm={() => { setConfirmTarget(null); void performApply(); }} /> : null}</>;
}
