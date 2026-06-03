/**
 * Plugin-local TypeScript types for the web-push subscribe panel.
 *
 * The panel talks only to the existing generic push-notification RPCs
 * (`push.backends.list`, `push.routes.list`, `push.routes.create`,
 * `push.routes.delete`) — no plugin-specific WS handlers required.
 * The server-side ``WebPush.runtime_data()`` exposes the VAPID public
 * key under ``backends[].runtime_data.vapid_public_key`` of the
 * ``push.backends.list`` response.
 */

export interface PushBackendEntry {
  name: string;
  label: string;
  enabled: boolean;
  destination_params: Array<{ key: string; description?: string }>;
  actions: Array<{ key: string; label: string }>;
  runtime_data: {
    vapid_public_key?: string;
    has_keys?: boolean;
    [k: string]: unknown;
  };
}

export interface PushBackendsListResult {
  type: "push.backends.list.result";
  ref?: string;
  ok: boolean;
  backends: PushBackendEntry[];
}

export interface PushRoute {
  _id: string;
  user_id: string;
  label: string;
  backend_name: string;
  destination_data: Record<string, unknown>;
  enabled: boolean;
  urgency_floor: string;
  created_at: string;
  updated_at: string;
}

export interface PushRoutesListResult {
  type: "push.routes.list.result";
  ref?: string;
  ok: boolean;
  routes: PushRoute[];
}

export interface PushRoutesCreateResult {
  type: "push.routes.create.result";
  ref?: string;
  ok: boolean;
  route: PushRoute;
}

export interface PushRoutesDeleteResult {
  type: "push.routes.delete.result";
  ref?: string;
  ok: boolean;
}

/**
 * Status the subscribe panel reports out to the user, in plain
 * English. Mapped from a combination of Notification.permission,
 * the presence of an existing PushSubscription, and whether that
 * subscription is registered as a route on the server.
 */
export type SubscribeUiState =
  | "loading"
  | "unsupported"
  | "permission-denied"
  | "ready-to-subscribe"
  | "subscribing"
  | "subscribed"
  | "no-vapid-key"
  | "error";
