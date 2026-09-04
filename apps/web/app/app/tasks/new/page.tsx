import Link from "next/link";

import { NewTaskForm } from "@/components/NewTaskForm";

export default function AppNewTaskPage() {
  return (
    <section className="panel stack">
      <div>
        <h1 style={{ margin: 0 }}>Create analytical task</h1>
        <p className="muted" style={{ marginTop: "0.35rem" }}>
          Submit task payload to <code>POST /api/tasks</code> and continue on task status page.
        </p>
      </div>

      <NewTaskForm />

      <div className="actions-row">
        <Link href="/app/tasks" className="secondary-button">
          Open task list
        </Link>
        <Link href="/" className="secondary-button">
          Home
        </Link>
      </div>
    </section>
  );
}
