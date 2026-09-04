import { TranslatedText } from "@/components/TranslatedText";
import Link from "next/link";
import { ProfileLocaleSync } from "@/components/ProfileLocaleSync";
import { requireCurrentUser } from "@/lib/auth/server";

export default async function ProfilePage() {
  const user = await requireCurrentUser("/app/profile");

  return (
    <section className="panel stack">
      <h2 style={{ margin: 0 }}>
        <TranslatedText id="page.profile.title" />
      </h2>
      <ProfileLocaleSync language={user.language} />
      <h3 style={{ marginBottom: 0 }}><TranslatedText id="profile.account-information" /></h3>
      <dl className="profile-details">
        <div>
          <dt>
            <TranslatedText id="auth.field.username" />
          </dt>
          <dd>{user.username}</dd>
        </div>
        <div>
          <dt>
            <TranslatedText id="auth.field.email" />
          </dt>
          <dd>{user.email}</dd>
        </div>
        <div>
          <dt>
            <TranslatedText id="auth.field.language" />
          </dt>
          <dd>{user.language}</dd>
        </div>
        <div><dt><TranslatedText id="profile.email-verified" /></dt><dd><TranslatedText id={user.email_verified ? "profile.email-verified" : "profile.email-unverified"} /></dd></div>
        <div><dt><TranslatedText id="profile.created" /></dt><dd><time dateTime={user.created_at}>{new Date(user.created_at).toLocaleString()}</time></dd></div>
      </dl>
      <Link href="/app/settings" className="secondary-button"><TranslatedText id="profile.open-settings" /></Link>
    </section>
  );
}
