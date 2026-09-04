"use client";

import { useEffect, useMemo, useState } from "react";

import { useI18n } from "@/components/I18nProvider";
import { useNotifications } from "@/components/notifications/NotificationProvider";
import {
  createTelegramLink,
  disconnectTelegram,
  getTelegramIntegrationState,
  getWebPushConfig,
  updateNotificationPreferences,
} from "@/lib/api/client";
import {
  notificationCategoryLabel,
  notificationEventLabel,
} from "@/lib/notifications/presentation";
import {
  accountRequiresDeviceNotifications,
  currentWebPushSupport,
  disableWebPushOnCurrentBrowser,
  enableWebPushOnCurrentBrowser,
  getCurrentBrowserPushSubscription,
  shouldRequestDevicePermissionAfterSave,
  type WebPushSupport,
} from "@/lib/notifications/push";
import type {
  NotificationPreferencesPatch,
  NotificationPreferencesResponse,
  WebNotificationChannel,
  WebPushConfigResponse,
  TelegramIntegrationState,
} from "@/types/api";
import { activeNotificationEventCatalog } from "../../../../packages/app-platform/src/notification-events";

const WEB_CHANNELS = ["in_app", "device", "email", "telegram"] as const;
const WEB_EVENTS = activeNotificationEventCatalog.filter(
  (event) => event.scope === "web" || event.scope === "both",
);

type SaveState = "idle" | "saving" | "saved" | "failed";
type DeviceActionState = "idle" | "working" | "enabled" | "denied" | "failed";
type TelegramActionState = "idle" | "working" | "failed";

export function NotificationSettings() {
  const { t } = useI18n();
  const { preferences, loadFailed, reload, replacePreferences } = useNotifications();
  const [baseline, setBaseline] = useState<NotificationPreferencesResponse | null>(null);
  const [draft, setDraft] = useState<NotificationPreferencesResponse | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [pushConfig, setPushConfig] = useState<WebPushConfigResponse | null>(null);
  const [pushConfigFailed, setPushConfigFailed] = useState(false);
  const [browserSubscribed, setBrowserSubscribed] = useState(false);
  const [pushSupport, setPushSupport] = useState<WebPushSupport>("unsupported");
  const [deviceAction, setDeviceAction] = useState<DeviceActionState>("idle");
  const [telegramState, setTelegramState] = useState<TelegramIntegrationState | null>(null);
  const [telegramAction, setTelegramAction] = useState<TelegramActionState>("idle");

  const dirty = Boolean(baseline && draft && !samePreferences(baseline, draft));
  const groups = useMemo(() => groupEvents(), []);

  useEffect(() => {
    if (!preferences || saveState === "saving") return;
    if (!draft || !dirty) {
      setBaseline(clonePreferences(preferences));
      setDraft(clonePreferences(preferences));
    }
  }, [dirty, draft, preferences, saveState]);

  useEffect(() => {
    let active = true;
    const support = currentWebPushSupport();
    setPushSupport(support);
    if (support !== "supported") return () => { active = false; };
    Promise.all([getWebPushConfig(), getCurrentBrowserPushSubscription()])
      .then(([config, subscription]) => {
        if (!active) return;
        setPushConfig(config);
        setBrowserSubscribed(
          Boolean(subscription && config.current_session_subscription_count > 0),
        );
      })
      .catch(() => {
        if (active) setPushConfigFailed(true);
      });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    const refresh = () => {
      getTelegramIntegrationState()
        .then((value) => {
          if (!active) return;
          setTelegramState(value);
          setTelegramAction("idle");
          void reload();
        })
        .catch(() => active && setTelegramAction("failed"));
    };
    refresh();
    window.addEventListener("focus", refresh);
    return () => {
      active = false;
      window.removeEventListener("focus", refresh);
    };
  }, [reload]);

  function updateDraft(transform: (current: NotificationPreferencesResponse) => NotificationPreferencesResponse) {
    setDraft((current) => (current ? transform(current) : current));
    setSaveState("idle");
  }

  async function refreshPushState() {
    const [config, subscription] = await Promise.all([
      getWebPushConfig(),
      getCurrentBrowserPushSubscription(),
    ]);
    setPushConfig(config);
    setBrowserSubscribed(Boolean(subscription && config.current_session_subscription_count > 0));
    setPushConfigFailed(false);
  }

  async function enableCurrentDevice(config = pushConfig) {
    if (!config) return;
    setDeviceAction("working");
    try {
      const result = await enableWebPushOnCurrentBrowser(config);
      if (result === "denied") {
        setDeviceAction("denied");
        return;
      }
      if (result === "unavailable") {
        setDeviceAction("failed");
        return;
      }
      await refreshPushState();
      setDeviceAction("enabled");
    } catch {
      setDeviceAction("failed");
    }
  }

  async function disableCurrentDevice() {
    setDeviceAction("working");
    try {
      await disableWebPushOnCurrentBrowser();
      await refreshPushState();
      setDeviceAction("idle");
    } catch {
      setDeviceAction("failed");
    }
  }

  async function connectTelegram() {
    const telegramWindow = window.open("about:blank", "_blank");
    if (telegramWindow) telegramWindow.opener = null;
    setTelegramAction("working");
    try {
      const link = await createTelegramLink();
      if (telegramWindow) telegramWindow.location.replace(link.url);
      else window.location.assign(link.url);
      setTelegramAction("idle");
    } catch {
      telegramWindow?.close();
      setTelegramAction("failed");
    }
  }

  async function removeTelegramLink() {
    setTelegramAction("working");
    try {
      await disconnectTelegram();
      const next = await getTelegramIntegrationState();
      setTelegramState(next);
      await reload();
      setTelegramAction("idle");
    } catch {
      setTelegramAction("failed");
    }
  }

  async function refreshTelegram() {
    setTelegramAction("working");
    try {
      const next = await getTelegramIntegrationState();
      setTelegramState(next);
      await reload();
      setTelegramAction("idle");
    } catch {
      setTelegramAction("failed");
    }
  }

  async function save() {
    if (!baseline || !draft || !dirty || saveState === "saving") return;
    setSaveState("saving");
    try {
      const saved = await updateNotificationPreferences(toPatch(draft));
      replacePreferences(saved);
      setBaseline(clonePreferences(saved));
      setDraft(clonePreferences(saved));
      setSaveState("saved");
      if (
        pushSupport === "supported" &&
        pushConfig?.available &&
        shouldRequestDevicePermissionAfterSave(baseline, saved, browserSubscribed)
      ) {
        await enableCurrentDevice(pushConfig);
      }
    } catch {
      setSaveState("failed");
    }
  }

  if (!draft) {
    return (
      <section className="panel stack">
        <h2 style={{ margin: 0 }}>{t("notification.settings.title")}</h2>
        {loadFailed ? (
          <div className="state-box state-error stack" role="alert">
            <span>{t("notification.settings.load-failed")}</span>
            <button type="button" className="secondary-button" onClick={() => void reload()}>
              {t("common.action.retry")}
            </button>
          </div>
        ) : (
          <p className="muted" aria-busy="true">{t("common.state.loading")}</p>
        )}
      </section>
    );
  }

  const accountDeviceRequired = accountRequiresDeviceNotifications(draft);

  return (
    <section className="panel stack notification-settings">
      <div>
        <h2 style={{ margin: 0 }}>{t("notification.settings.title")}</h2>
        <p className="muted">{t("notification.settings.description")}</p>
      </div>

      <div className="notification-master-settings">
        <PreferenceToggle
          label={t("notification.settings.global")}
          checked={draft.enabled}
          disabled={saveState === "saving"}
          onChange={(enabled) => updateDraft((current) => ({ ...current, enabled }))}
        />
        <PreferenceToggle
          label={t("notification.settings.sound")}
          checked={draft.sound_enabled}
          disabled={saveState === "saving"}
          onChange={(sound_enabled) => updateDraft((current) => ({ ...current, sound_enabled }))}
        />
      </div>

      <fieldset className="notification-channel-settings">
        <legend>{t("notification.settings.channels")}</legend>
        {WEB_CHANNELS.map((channel) => {
          const item = draft.channels.find((candidate) => candidate.channel === channel);
          if (!item) return null;
          return (
            <div className="notification-channel-row" key={channel}>
              <PreferenceToggle
                label={channelLabel(channel, t)}
                checked={item.enabled}
                disabled={saveState === "saving"}
                onChange={(enabled) =>
                  updateDraft((current) => ({
                    ...current,
                    channels: current.channels.map((candidate) =>
                      candidate.channel === channel ? { ...candidate, enabled } : candidate,
                    ),
                  }))
                }
              />
              <span className="muted">
                {channelAvailability(channel, item.available, telegramState, t)}
              </span>
            </div>
          );
        })}
      </fieldset>

      <div className="stack notification-device-settings">
        <h3>{t("notification.device.current-browser")}</h3>
        <DeviceStatus
          support={pushSupport}
          config={pushConfig}
          configFailed={pushConfigFailed}
          subscribed={browserSubscribed}
          action={deviceAction}
        />
        <div className="actions-row">
          {pushSupport === "supported" && accountDeviceRequired && !browserSubscribed ? (
            <button
              type="button"
              className="secondary-button"
              disabled={deviceAction === "working" || !pushConfig?.available}
              onClick={() => void enableCurrentDevice()}
            >
              {deviceAction === "working"
                ? t("common.state.loading")
                : t("notification.device.enable-current")}
            </button>
          ) : null}
          {pushSupport === "supported" && browserSubscribed ? (
            <button
              type="button"
              className="secondary-button"
              disabled={deviceAction === "working"}
              onClick={() => void disableCurrentDevice()}
            >
              {deviceAction === "working"
                ? t("common.state.loading")
                : t("notification.device.disable-current")}
            </button>
          ) : null}
        </div>
      </div>

      <div className="stack notification-device-settings">
        <h3>{t("notification.telegram.connection")}</h3>
        <TelegramStatus state={telegramState} action={telegramAction} />
        <div className="actions-row">
          {telegramState?.integration_available && !telegramState.linked ? (
            <button type="button" className="secondary-button" disabled={telegramAction === "working"} onClick={() => void connectTelegram()}>
              {telegramAction === "working" ? t("common.state.loading") : t("notification.telegram.connect")}
            </button>
          ) : null}
          {telegramState?.linked ? (
            <button type="button" className="danger-button" disabled={telegramAction === "working"} onClick={() => void removeTelegramLink()}>
              {telegramAction === "working" ? t("common.state.loading") : t("notification.telegram.disconnect")}
            </button>
          ) : null}
          {telegramState?.integration_available ? (
            <button type="button" className="secondary-button" disabled={telegramAction === "working"} onClick={() => void refreshTelegram()}>
              {t("notification.telegram.refresh")}
            </button>
          ) : null}
        </div>
      </div>

      <div className="notification-matrix-scroll" tabIndex={0}>
        <table className="notification-matrix">
          <caption>{t("notification.settings.matrix-caption")}</caption>
          <thead>
            <tr>
              <th scope="col">{t("notification.settings.event")}</th>
              {WEB_CHANNELS.map((channel) => <th scope="col" key={channel}>{channelLabel(channel, t)}</th>)}
            </tr>
          </thead>
          <tbody>
            {groups.map(([category, events]) => [
              <tr className="notification-matrix-group" key={`${category}-heading`}>
                <th scope="rowgroup" colSpan={WEB_CHANNELS.length + 1}>
                  {notificationCategoryLabel(category, t)}
                </th>
              </tr>,
              ...events.map((event) => {
                const preference = draft.events.find((item) => item.event_id === event.id);
                return (
                  <tr key={event.id}>
                    <th scope="row">{notificationEventLabel(event.id, t)}</th>
                    {WEB_CHANNELS.map((channel) => {
                      const applicable = preference?.channels.includes(channel) ?? false;
                      return (
                        <td key={channel}>
                          {applicable && preference ? (
                            <label className="notification-matrix-checkbox">
                              <input
                                type="checkbox"
                                checked={preference.enabled[channel] ?? preference.default_enabled}
                                disabled={saveState === "saving"}
                                onChange={(input) =>
                                  updateDraft((current) => ({
                                    ...current,
                                    events: current.events.map((item) =>
                                      item.event_id === event.id
                                        ? {
                                            ...item,
                                            enabled: { ...item.enabled, [channel]: input.target.checked },
                                          }
                                        : item,
                                    ),
                                  }))
                                }
                              />
                              <span className="visually-hidden">
                                {notificationEventLabel(event.id, t)} · {channelLabel(channel, t)}
                              </span>
                            </label>
                          ) : (
                            <span className="muted" aria-label={t("notification.settings.not-applicable")}>—</span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                );
              }),
            ])}
          </tbody>
        </table>
      </div>

      {saveState === "saved" ? <p className="state-box" role="status">{t("notification.settings.saved")}</p> : null}
      {saveState === "failed" ? <p className="state-box state-error" role="alert">{t("notification.settings.save-failed")}</p> : null}
      <div className="actions-row">
        <button
          type="button"
          className="primary-button"
          disabled={!dirty || saveState === "saving"}
          onClick={() => void save()}
        >
          {saveState === "saving" ? t("common.state.loading") : t("common.action.save")}
        </button>
      </div>
    </section>
  );
}

function PreferenceToggle({ label, checked, disabled, onChange }: Readonly<{ label: string; checked: boolean; disabled: boolean; onChange: (checked: boolean) => void }>) {
  return <label className="notification-toggle"><input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} /><span>{label}</span><strong>{checked ? "ON" : "OFF"}</strong></label>;
}

function DeviceStatus({ support, config, configFailed, subscribed, action }: Readonly<{ support: WebPushSupport; config: WebPushConfigResponse | null; configFailed: boolean; subscribed: boolean; action: DeviceActionState }>) {
  const { t } = useI18n();
  if (support === "unsupported_safari_ios") return <p className="state-box state-warning">{t("notification.device.safari-ios-unsupported")}</p>;
  if (support === "insecure_context") return <p className="state-box state-warning">{t("notification.device.secure-context-required")}</p>;
  if (support === "unsupported") return <p className="state-box state-warning">{t("notification.device.browser-unsupported")}</p>;
  if (configFailed) return <p className="state-box state-error">{t("notification.device.config-failed")}</p>;
  if (!config) return <p className="muted" aria-busy="true">{t("common.state.loading")}</p>;
  if (!config.available) return <p className="state-box state-warning">{t("notification.device.server-unavailable")}</p>;
  if (action === "denied" || window.Notification.permission === "denied") return <p className="state-box state-warning">{t("notification.device.permission-denied")}</p>;
  if (action === "failed") return <p className="state-box state-error">{t("notification.device.action-failed")}</p>;
  return <p className="muted">{subscribed ? t("notification.device.enabled-current") : t("notification.device.not-enabled-current")} {t("notification.device.active-count").replace("{count}", String(config.active_subscription_count))}</p>;
}

function TelegramStatus({ state, action }: Readonly<{ state: TelegramIntegrationState | null; action: TelegramActionState }>) {
  const { t } = useI18n();
  if (action === "failed") return <p className="state-box state-error">{t("notification.telegram.action-failed")}</p>;
  if (!state) return <p className="muted" aria-busy="true">{t("common.state.loading")}</p>;
  if (!state.integration_available) return <p className="state-box state-warning">{t("notification.telegram.server-unavailable")}</p>;
  if (!state.linked) return <p className="muted">{t("notification.telegram.not-connected")}</p>;
  const label = state.display_name || (state.username ? `@${state.username}` : "Telegram");
  return <p className="muted">{t("notification.telegram.connected").replace("{account}", label)}</p>;
}

function groupEvents() {
  const groups = new Map<string, typeof WEB_EVENTS[number][]>();
  WEB_EVENTS.forEach((event) => groups.set(event.category, [...(groups.get(event.category) ?? []), event]));
  return [...groups.entries()];
}

function channelLabel(channel: WebNotificationChannel, t: ReturnType<typeof useI18n>["t"]): string {
  const keys = { in_app: "notification.channel.in-app", device: "notification.channel.device", email: "notification.channel.email", telegram: "notification.channel.telegram" } as const;
  return t(keys[channel]);
}

function channelAvailability(channel: WebNotificationChannel, available: boolean, telegramState: TelegramIntegrationState | null, t: ReturnType<typeof useI18n>["t"]): string {
  if (channel === "email" && !available) return t("notification.channel.email-unavailable");
  if (channel === "telegram") {
    if (!telegramState?.integration_available) return t("notification.telegram.server-unavailable");
    return t(telegramState.linked ? "notification.channel.available" : "notification.telegram.not-connected");
  }
  return t(available ? "notification.channel.available" : "notification.channel.unavailable");
}

function clonePreferences(value: NotificationPreferencesResponse): NotificationPreferencesResponse {
  return {
    ...value,
    channels: value.channels.map((item) => ({ ...item })),
    events: value.events.map((item) => ({ ...item, channels: [...item.channels], enabled: { ...item.enabled }, effective: { ...item.effective } })),
  };
}

function samePreferences(left: NotificationPreferencesResponse, right: NotificationPreferencesResponse): boolean {
  return JSON.stringify(toPatch(left)) === JSON.stringify(toPatch(right));
}

function toPatch(value: NotificationPreferencesResponse): NotificationPreferencesPatch {
  return {
    enabled: value.enabled,
    sound_enabled: value.sound_enabled,
    channels: Object.fromEntries(WEB_CHANNELS.map((channel) => [channel, value.channels.find((item) => item.channel === channel)?.enabled ?? false])) as Partial<Record<WebNotificationChannel, boolean>>,
    events: value.events.flatMap((event) => event.channels.filter((channel): channel is WebNotificationChannel => WEB_CHANNELS.includes(channel as WebNotificationChannel)).map((channel) => ({ event_id: event.event_id, channel, enabled: event.enabled[channel] ?? event.default_enabled }))),
  };
}
