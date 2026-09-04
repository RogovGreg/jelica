import Link from "next/link";
import type { Metadata } from "next";
import { ResetPasswordForm } from "@/components/auth/ResetPasswordForm";
import { TranslatedText } from "@/components/TranslatedText";

export const metadata: Metadata = { referrer: "no-referrer" };

export default function ResetPasswordPage() {
  return <section className="panel stack"><h2 style={{ margin: 0 }}><TranslatedText id="auth.reset.title" /></h2><ResetPasswordForm /><div className="actions-row"><Link href="/auth/login" className="secondary-button"><TranslatedText id="auth.action.login" /></Link><Link href="/auth/register" className="secondary-button"><TranslatedText id="auth.action.register" /></Link></div></section>;
}
