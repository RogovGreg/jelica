import Link from "next/link";

import { ForgotPasswordForm } from "@/components/auth/ForgotPasswordForm";
import { TranslatedText } from "@/components/TranslatedText";

export default function ForgotPasswordPage() {
  return <section className="panel stack"><h2 style={{ margin: 0 }}><TranslatedText id="auth.forgot.title" /></h2><p className="muted"><TranslatedText id="auth.forgot.description" /></p><ForgotPasswordForm /><Link href="/auth/login" className="secondary-button"><TranslatedText id="auth.action.login" /></Link></section>;
}
