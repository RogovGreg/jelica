import catalog from "../../contracts/src/jelica_contracts/notification_events.json";

export type NotificationEventScope = "local" | "web" | "both";

export type NotificationEventDefinition = {
  readonly id: string;
  readonly category: string;
  readonly scope: NotificationEventScope;
  readonly default_enabled: boolean;
  readonly channels: readonly string[];
  readonly supersedes?: readonly string[];
  readonly active?: boolean;
};

export const notificationEventCatalog = catalog.events as readonly NotificationEventDefinition[];

export const activeNotificationEventCatalog = notificationEventCatalog.filter(
  (event) => event.active !== false,
);
