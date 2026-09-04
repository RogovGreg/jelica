import { RegisterForm } from "@/components/auth/RegisterForm";
import { TranslatedText } from "@/components/TranslatedText";

export default function RegisterPage() {
  return (
    <section className="panel stack">
      <h2 style={{ margin: 0 }}>
        <TranslatedText id="auth.register.title" />
      </h2>
      <RegisterForm />
    </section>
  );
}
