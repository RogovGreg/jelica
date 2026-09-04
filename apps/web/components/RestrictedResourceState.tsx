"use client";

import Link from "next/link";

import { useI18n } from "@/components/I18nProvider";

type ResourceType = "project" | "task" | "result";
type RestrictedVariant = "resource-unavailable" | "access-denied";

export function RestrictedResourceState({ variant, resourceType }: Readonly<{ variant: RestrictedVariant; resourceType: ResourceType }>) {
  const { t } = useI18n();
  const isUnavailable = variant === "resource-unavailable";
  const descriptionKey = isUnavailable ? "error.resourceUnavailable.description" : ({ project: "error.accessDenied.projectDescription", task: "error.accessDenied.taskDescription", result: "error.accessDenied.resultDescription" } as const)[resourceType];
  const list = ({ project: { href: "/app/projects", key: "project.action.backToProjects" }, task: { href: "/app/tasks", key: "task.action.backToTasks" }, result: { href: "/app/results", key: "result.action.backToResults" } } as const)[resourceType];
  return <section className="panel stack restricted-resource-state" role="alert">
    <span className="restricted-resource-code" aria-hidden="true">{isUnavailable ? "404" : "403"}</span>
    <h1 style={{ margin: 0 }}>{t(isUnavailable ? "error.resourceUnavailable.title" : "error.accessDenied.title")}</h1>
    <div className="state-box">{t(descriptionKey)}</div>
    <div className="actions-row"><Link href={list.href} className="primary-button">{t(list.key)}</Link></div>
  </section>;
}
