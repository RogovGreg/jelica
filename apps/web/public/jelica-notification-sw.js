"use strict";

const FALLBACK_TARGET = "/app/notifications";

self.addEventListener("push", (event) => {
  const payload = parsePushPayload(event.data);
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      tag: `jelica-notification-${payload.notificationId ?? "unknown"}`,
      data: {
        notificationId: payload.notificationId,
        targetPath: payload.targetPath,
      },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const notificationId = validNotificationId(event.notification.data?.notificationId)
    ? event.notification.data.notificationId
    : null;
  const targetPath = safeTargetPath(event.notification.data?.targetPath)
    ? event.notification.data.targetPath
    : FALLBACK_TARGET;
  event.waitUntil(openNotification(notificationId, targetPath));
});

async function openNotification(notificationId, targetPath) {
  if (notificationId) {
    try {
      await fetch(`/api/notifications/${encodeURIComponent(notificationId)}`, {
        method: "PATCH",
        credentials: "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ read: true }),
      });
    } catch {
      // Authentication/read-state failure must not block opening JELICA.
    }
  }
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const client of windows) {
    if (new URL(client.url).origin !== self.location.origin) continue;
    if ("navigate" in client) await client.navigate(targetPath);
    return client.focus();
  }
  return self.clients.openWindow(targetPath);
}

function parsePushPayload(data) {
  try {
    const value = data?.json();
    if (!value || typeof value !== "object") return fallbackPayload();
    const notificationId = validNotificationId(value.notification_id)
      ? value.notification_id
      : null;
    if (
      !notificationId ||
      typeof value.title !== "string" ||
      value.title.trim() === "" ||
      value.title.length > 160 ||
      typeof value.body !== "string" ||
      value.body.length > 320 ||
      !safeTargetPath(value.target_path)
    ) {
      return fallbackPayload();
    }
    return {
      notificationId,
      title: value.title,
      body: value.body,
      targetPath: value.target_path,
    };
  } catch {
    return fallbackPayload();
  }
}

function fallbackPayload() {
  return {
    notificationId: null,
    title: "JELICA",
    body: "",
    targetPath: FALLBACK_TARGET,
  };
}

function validNotificationId(value) {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)
  );
}

function safeTargetPath(value) {
  if (typeof value !== "string" || !value.startsWith("/app") || value.startsWith("//")) return false;
  if (value.includes("\\") || /[\u0000-\u001f\u007f]/.test(value)) return false;
  try {
    const parsed = new URL(value, self.location.origin);
    return (
      parsed.origin === self.location.origin &&
      (parsed.pathname === "/app" || parsed.pathname.startsWith("/app/")) &&
      parsed.username === "" &&
      parsed.password === ""
    );
  } catch {
    return false;
  }
}
