import { LoginForm } from "@/components/auth/LoginForm";
import { TranslatedText } from "@/components/TranslatedText";

type LoginPageProps = {
  searchParams?: {
    next?: string | string[];
  };
};

export default function LoginPage({ searchParams }: LoginPageProps) {
  const nextPath = safeNextPath(firstValue(searchParams?.next));

  return (
    <section className="panel stack">
      <h2 style={{ margin: 0 }}>
        <TranslatedText id="auth.login.title" />
      </h2>
      <LoginForm nextPath={nextPath} />
    </section>
  );
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function safeNextPath(value: string | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) {
    return "/app/tasks";
  }
  return value;
}
