import { SupportRequestForm } from "@/components/SupportRequestForm";

export default function AppSupportPage() {
  return (
    <section className="panel stack">
      <div>
        <h1 style={{ margin: 0 }}>Support</h1>
        <p className="muted" style={{ marginTop: "0.35rem" }}>
          Submit a support request and receive a request ID for follow-up.
        </p>
      </div>

      <SupportRequestForm />
    </section>
  );
}
