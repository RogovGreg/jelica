import { TranslatedText } from "@/components/TranslatedText";
import { VerifyEmailForm } from "@/components/auth/VerifyEmailForm";
import type { Metadata } from "next";

export const metadata: Metadata = { referrer: "no-referrer" };

type VerifyEmailPageProps = {
  searchParams?: {
    registered?: string | string[];
  };
};

export default function VerifyEmailPage({ searchParams }: VerifyEmailPageProps) {
  return (
    <section className="panel stack">
      <h2 style={{ margin: 0 }}>
        <TranslatedText id="auth.verify.title" />
      </h2>
      <VerifyEmailForm
        registrationComplete={firstValue(searchParams?.registered) === "1"}
      />
    </section>
  );
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}
