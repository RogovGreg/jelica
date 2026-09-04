import { SupportRequestForm } from "@/components/SupportRequestForm";
import { TranslatedText } from "@/components/TranslatedText";

export default function SupportPage() {
  return (
    <section className="panel stack">
      <div>
        <h1 style={{ margin: 0 }}><TranslatedText id="public.support.title" /></h1>
        <p className="muted" style={{ marginTop: "0.35rem" }}>
          <TranslatedText id="public.support.description" />
        </p>
      </div>
      <SupportRequestForm />
    </section>
  );
}
