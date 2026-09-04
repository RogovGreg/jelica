"use client";

import { usePathname } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  getNotificationPreferences,
  getNotifications,
  markAllNotificationsRead,
  setNotificationReadState,
} from "@/lib/api/client";
import { toApiClientError } from "@/lib/api/errors";
import { notificationMatchesCurrentContext } from "@/lib/notifications/presentation";
import { playNotificationSound } from "@/lib/notifications/push";
import { parseNotificationRealtimeMessage } from "@/lib/notifications/realtime";
import {
  applyNotificationRealtimeEvent,
  installNotificationSnapshot,
  unreadNotificationCount,
} from "@/lib/notifications/state";
import type {
  NotificationPreferencesResponse,
  NotificationRealtimeMessage,
  WebNotification,
} from "@/types/api";

const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 10000] as const;
const TOAST_LIFETIME_MS = 8000;

export type NotificationToast = { id: string; notification: WebNotification };

type NotificationContextValue = {
  notifications: WebNotification[];
  preferences: NotificationPreferencesResponse | null;
  loading: boolean;
  loadFailed: boolean;
  unreadCount: number;
  badgeVisible: boolean;
  toasts: NotificationToast[];
  reload: () => Promise<void>;
  markReadState: (notificationId: string, read: boolean) => Promise<void>;
  markAllRead: () => Promise<void>;
  replacePreferences: (preferences: NotificationPreferencesResponse) => void;
  dismissToast: (toastId: string) => void;
};

const NotificationContext = createContext<NotificationContextValue | null>(null);

export function NotificationProvider({ children }: Readonly<{ children: ReactNode }>) {
  const pathname = usePathname();
  const [notifications, setNotifications] = useState<WebNotification[]>([]);
  const [preferences, setPreferences] = useState<NotificationPreferencesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [toasts, setToasts] = useState<NotificationToast[]>([]);
  const notificationsRef = useRef<WebNotification[]>([]);
  const preferencesRef = useRef<NotificationPreferencesResponse | null>(null);
  const pathnameRef = useRef(pathname);
  const snapshotLoaderRef = useRef<(() => Promise<void>) | null>(null);
  const toastTimersRef = useRef(new Map<string, number>());

  useEffect(() => {
    pathnameRef.current = pathname;
  }, [pathname]);

  const replaceNotifications = useCallback((next: WebNotification[]) => {
    notificationsRef.current = next;
    setNotifications(next);
  }, []);

  const replacePreferences = useCallback((next: NotificationPreferencesResponse) => {
    preferencesRef.current = next;
    setPreferences(next);
  }, []);

  const dismissToast = useCallback((toastId: string) => {
    const timer = toastTimersRef.current.get(toastId);
    if (timer !== undefined) window.clearTimeout(timer);
    toastTimersRef.current.delete(toastId);
    setToasts((items) => items.filter((item) => item.id !== toastId));
  }, []);

  const presentRealtimeNotification = useCallback(
    (notification: WebNotification, currentPreferences: NotificationPreferencesResponse) => {
      const inApp = currentPreferences.channels.find((item) => item.channel === "in_app");
      if (
        !currentPreferences.enabled ||
        !inApp?.enabled ||
        notificationMatchesCurrentContext(notification, pathnameRef.current)
      ) {
        return;
      }
      const toastId = notification.id;
      setToasts((items) => [
        ...items.filter((item) => item.id !== toastId).slice(-2),
        { id: toastId, notification },
      ]);
      const previousTimer = toastTimersRef.current.get(toastId);
      if (previousTimer !== undefined) window.clearTimeout(previousTimer);
      toastTimersRef.current.set(
        toastId,
        window.setTimeout(() => dismissToast(toastId), TOAST_LIFETIME_MS),
      );
      if (currentPreferences.sound_enabled) void playNotificationSound();
    },
    [dismissToast],
  );

  useEffect(() => {
    const toastTimers = toastTimersRef.current;
    let cancelled = false;
    let terminal = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let reconnectAttempt = 0;
    let generation = 0;
    let buffering = false;
    let buffer: NotificationRealtimeMessage[] = [];
    let presentationBuffer: WebNotification[] = [];

    const loadSnapshot = async () => {
      const loadGeneration = generation;
      setLoading(true);
      setLoadFailed(false);
      try {
        const [list, nextPreferences] = await Promise.all([
          getNotifications(),
          getNotificationPreferences(),
        ]);
        if (cancelled || terminal || loadGeneration !== generation) return;
        const buffered = buffer.splice(0);
        const queuedPresentations = presentationBuffer.splice(0);
        buffering = false;
        replaceNotifications(installNotificationSnapshot(list.items, buffered));
        replacePreferences(nextPreferences);
        queuedPresentations.forEach((notification) =>
          presentRealtimeNotification(notification, nextPreferences),
        );
        reconnectAttempt = 0;
      } catch (error) {
        if (cancelled || loadGeneration !== generation) return;
        const apiError = toApiClientError(error);
        if (apiError.status === 401) {
          terminal = true;
          socket?.close(1000);
          replaceNotifications([]);
          preferencesRef.current = null;
          setPreferences(null);
        } else {
          const buffered = buffer.splice(0);
          buffering = false;
          replaceNotifications(
            buffered.reduce(
              (items, event) => applyNotificationRealtimeEvent(items, event),
              notificationsRef.current,
            ),
          );
          setLoadFailed(true);
        }
      } finally {
        if (!cancelled && loadGeneration === generation) setLoading(false);
      }
    };
    snapshotLoaderRef.current = loadSnapshot;

    const scheduleReconnect = () => {
      if (cancelled || terminal || reconnectTimer !== null) return;
      const delay = RECONNECT_DELAYS_MS[Math.min(reconnectAttempt, RECONNECT_DELAYS_MS.length - 1)];
      reconnectAttempt += 1;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };

    const connect = () => {
      if (cancelled || terminal || socket) return;
      generation += 1;
      buffering = true;
      buffer = [];
      presentationBuffer = [];
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const currentSocket = new WebSocket(
        `${protocol}//${window.location.host}/api/notifications/realtime`,
      );
      socket = currentSocket;
      currentSocket.onopen = () => {
        if (!cancelled && !terminal && socket === currentSocket) void loadSnapshot();
      };
      currentSocket.onmessage = (event) => {
        if (typeof event.data !== "string") return;
        const message = parseNotificationRealtimeMessage(event.data);
        if (!message) return;
        if (buffering) buffer.push(message);
        else replaceNotifications(applyNotificationRealtimeEvent(notificationsRef.current, message));
        if (message.type === "notification.created") {
          const currentPreferences = preferencesRef.current;
          if (buffering || !currentPreferences) presentationBuffer.push(message.notification);
          else presentRealtimeNotification(message.notification, currentPreferences);
        }
      };
      currentSocket.onclose = (event) => {
        if (socket === currentSocket) socket = null;
        if (cancelled || terminal) return;
        buffering = false;
        if (event.code === 4401) {
          terminal = true;
          replaceNotifications([]);
          preferencesRef.current = null;
          setPreferences(null);
          return;
        }
        scheduleReconnect();
      };
    };

    connect();
    return () => {
      cancelled = true;
      terminal = true;
      snapshotLoaderRef.current = null;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      for (const timer of toastTimers.values()) window.clearTimeout(timer);
      toastTimers.clear();
      const currentSocket = socket;
      socket = null;
      if (currentSocket && currentSocket.readyState < WebSocket.CLOSING) currentSocket.close(1000);
    };
  }, [presentRealtimeNotification, replaceNotifications, replacePreferences]);

  const reload = useCallback(async () => {
    await snapshotLoaderRef.current?.();
  }, []);

  const markReadState = useCallback(
    async (notificationId: string, read: boolean) => {
      const current = notificationsRef.current.find((item) => item.id === notificationId);
      if (!current || (read ? current.read_at !== null : current.read_at === null)) return;
      const optimisticReadAt = read ? new Date().toISOString() : null;
      replaceNotifications(
        notificationsRef.current.map((item) =>
          item.id === notificationId ? { ...item, read_at: optimisticReadAt } : item,
        ),
      );
      try {
        const updated = await setNotificationReadState(notificationId, read);
        replaceNotifications(
          notificationsRef.current.map((item) => (item.id === updated.id ? updated : item)),
        );
      } catch (error) {
        replaceNotifications(
          notificationsRef.current.map((item) =>
            item.id === notificationId ? { ...item, read_at: current.read_at } : item,
          ),
        );
        throw error;
      }
    },
    [replaceNotifications],
  );

  const markAllRead = useCallback(async () => {
    const previous = notificationsRef.current;
    const readAt = new Date().toISOString();
    replaceNotifications(previous.map((item) => ({ ...item, read_at: item.read_at ?? readAt })));
    try {
      await markAllNotificationsRead();
    } catch (error) {
      replaceNotifications(previous);
      throw error;
    }
  }, [replaceNotifications]);

  const unreadCount = unreadNotificationCount(notifications);
  const inAppEnabled = preferences?.channels.find((item) => item.channel === "in_app")?.enabled;
  const value = useMemo<NotificationContextValue>(
    () => ({
      notifications,
      preferences,
      loading,
      loadFailed,
      unreadCount,
      badgeVisible: Boolean(preferences?.enabled && inAppEnabled && unreadCount > 0),
      toasts,
      reload,
      markReadState,
      markAllRead,
      replacePreferences,
      dismissToast,
    }),
    [
      dismissToast,
      inAppEnabled,
      loadFailed,
      loading,
      markAllRead,
      markReadState,
      notifications,
      preferences,
      reload,
      replacePreferences,
      toasts,
      unreadCount,
    ],
  );
  return <NotificationContext.Provider value={value}>{children}</NotificationContext.Provider>;
}

export function useNotifications(): NotificationContextValue {
  const value = useContext(NotificationContext);
  if (!value) throw new Error("useNotifications must be used within NotificationProvider");
  return value;
}
