import {
  deleteCurrentWebPushSubscription,
  registerWebPushSubscription,
} from "@/lib/api/client";
import type {
  NotificationPreferencesResponse,
  WebPushConfigResponse,
  WebPushSubscriptionPayload,
} from "@/types/api";

export const NOTIFICATION_SERVICE_WORKER_PATH = "/jelica-notification-sw.js";
export const NOTIFICATION_SOUND_RUNTIME_PATH = "/assets/notification.wav";

export type WebPushSupport =
  | "supported"
  | "insecure_context"
  | "unsupported"
  | "unsupported_safari_ios";

export type PushBrowserCapabilities = {
  secureContext: boolean;
  userAgent: string;
  platform: string;
  maxTouchPoints: number;
  serviceWorker: boolean;
  pushManager: boolean;
  notification: boolean;
};

export function detectWebPushSupport(
  capabilities: PushBrowserCapabilities,
): WebPushSupport {
  const userAgent = capabilities.userAgent.toLowerCase();
  const isiPadDesktopMode =
    capabilities.platform === "MacIntel" && capabilities.maxTouchPoints > 1;
  const isiOS = /iphone|ipad|ipod/.test(userAgent) || isiPadDesktopMode;
  const isSafari =
    userAgent.includes("safari") &&
    !userAgent.includes("chrome") &&
    !userAgent.includes("chromium") &&
    !userAgent.includes("crios") &&
    !userAgent.includes("android") &&
    !userAgent.includes("firefox") &&
    !userAgent.includes("fxios");
  if (isiOS || isSafari) return "unsupported_safari_ios";
  if (!capabilities.secureContext) return "insecure_context";
  if (
    !capabilities.serviceWorker ||
    !capabilities.pushManager ||
    !capabilities.notification
  ) {
    return "unsupported";
  }
  return "supported";
}

export function currentWebPushSupport(): WebPushSupport {
  return detectWebPushSupport({
    secureContext: window.isSecureContext,
    userAgent: window.navigator.userAgent,
    platform: window.navigator.platform,
    maxTouchPoints: window.navigator.maxTouchPoints,
    serviceWorker: "serviceWorker" in window.navigator,
    pushManager: "PushManager" in window,
    notification: "Notification" in window,
  });
}

export function accountRequiresDeviceNotifications(
  preferences: NotificationPreferencesResponse,
): boolean {
  const device = preferences.channels.find((item) => item.channel === "device");
  return Boolean(
    preferences.enabled &&
      device?.enabled &&
      preferences.events.some(
        (event) => event.channels.includes("device") && event.enabled.device === true,
      ),
  );
}

export function shouldRequestDevicePermissionAfterSave(
  before: NotificationPreferencesResponse,
  after: NotificationPreferencesResponse,
  currentBrowserSubscribed: boolean,
): boolean {
  return (
    !currentBrowserSubscribed &&
    !accountRequiresDeviceNotifications(before) &&
    accountRequiresDeviceNotifications(after)
  );
}

export async function getCurrentBrowserPushSubscription(): Promise<PushSubscription | null> {
  if (currentWebPushSupport() !== "supported") return null;
  const registration = await window.navigator.serviceWorker.getRegistration();
  return registration?.pushManager.getSubscription() ?? null;
}

export async function enableWebPushOnCurrentBrowser(
  config: WebPushConfigResponse,
): Promise<"enabled" | "denied" | "unavailable"> {
  if (
    currentWebPushSupport() !== "supported" ||
    !config.available ||
    !config.vapid_public_key
  ) {
    return "unavailable";
  }
  let permission = window.Notification.permission;
  if (permission === "default") permission = await window.Notification.requestPermission();
  if (permission !== "granted") return "denied";

  const registration = await window.navigator.serviceWorker.register(
    NOTIFICATION_SERVICE_WORKER_PATH,
    { scope: "/" },
  );
  const subscription =
    (await registration.pushManager.getSubscription()) ??
    (await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: decodeVapidPublicKey(config.vapid_public_key),
    }));
  await registerWebPushSubscription(serializePushSubscription(subscription));
  return "enabled";
}

export async function disableWebPushOnCurrentBrowser(): Promise<void> {
  const subscription = await getCurrentBrowserPushSubscription();
  if (!subscription) return;
  await deleteCurrentWebPushSubscription(subscription.endpoint);
  try {
    await subscription.unsubscribe();
  } catch {
    // Server-side deletion is authoritative; browser cleanup is best-effort.
  }
}

export async function playNotificationSound(): Promise<boolean> {
  try {
    const audio = new Audio(NOTIFICATION_SOUND_RUNTIME_PATH);
    await audio.play();
    return true;
  } catch {
    return false;
  }
}

function serializePushSubscription(
  subscription: PushSubscription,
): WebPushSubscriptionPayload {
  const serialized = subscription.toJSON();
  const p256dh = serialized.keys?.p256dh;
  const auth = serialized.keys?.auth;
  if (!p256dh || !auth) throw new Error("Push subscription keys are unavailable.");
  return {
    endpoint: subscription.endpoint,
    expiration_time: subscription.expirationTime,
    keys: { p256dh, auth },
  };
}

function decodeVapidPublicKey(value: string): ArrayBuffer {
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  const decoded = window.atob(padded);
  const bytes = new Uint8Array(decoded.length);
  for (let index = 0; index < decoded.length; index += 1) {
    bytes[index] = decoded.charCodeAt(index);
  }
  return bytes.buffer;
}
