import { TranslatedText } from "@/components/TranslatedText";
import { AccountSettings } from "@/components/AccountSettings";
import { getCurrentUserIfPresent } from "@/lib/auth/server";
import { discoverUiLocales } from "@/lib/i18n/discovery";

export default async function SettingsPage() {
  const user = await getCurrentUserIfPresent();

  return (
    <section className="panel stack">
      <h2 style={{ margin: 0 }}>
        <TranslatedText id="page.settings.title" />
      </h2>
      <AccountSettings user={user} locales={discoverUiLocales()} />
    </section>
  );
}
