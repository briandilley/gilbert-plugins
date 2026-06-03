/**
 * Plugin-local hook for the web-push subscribe panel.
 *
 * Uses ONLY the existing generic push-notification WS RPCs
 * (`push.backends.list`, `push.routes.list`, `push.routes.create`,
 * `push.routes.delete`) — no plugin-specific server-side plumbing.
 * The server's VAPID public key is surfaced via
 * ``WebPush.runtime_data()`` in the ``push.backends.list`` response.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { useWebSocket } from "@/hooks/useWebSocket";

import type {
  PushBackendEntry,
  PushBackendsListResult,
  PushRoute,
  PushRoutesCreateResult,
  PushRoutesDeleteResult,
  PushRoutesListResult,
} from "./types";

const BACKEND_NAME = "web_push";

/**
 * Convert a base64url-no-pad VAPID public key (as the server returns
 * it) to the ``Uint8Array`` shape ``PushManager.subscribe`` requires
 * for ``applicationServerKey``.
 */
function vapidPublicKeyToUint8(key: string): Uint8Array<ArrayBuffer> {
  // Re-add padding the server stripped.
  const padded = key + "=".repeat((4 - (key.length % 4)) % 4);
  const base64 = padded.replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  // TS 6 made ``Uint8Array`` generic over its backing buffer
  // (``ArrayBuffer`` vs ``SharedArrayBuffer``). ``PushManager.subscribe``
  // wants ``BufferSource`` over plain ``ArrayBuffer`` specifically, so
  // construct the buffer explicitly rather than letting ``new
  // Uint8Array(N)`` widen to ``ArrayBufferLike``.
  const buffer = new ArrayBuffer(raw.length);
  const arr = new Uint8Array(buffer);
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return arr;
}

/**
 * Re-encode a binary ``ArrayBuffer`` (PushSubscription.getKey returns
 * one) as base64url-no-pad — the form the server stores on the route.
 */
function arrayBufferToBase64Url(buffer: ArrayBuffer | null): string {
  if (buffer === null) return "";
  const bytes = new Uint8Array(buffer);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

/**
 * Build a friendly device label from ``navigator.userAgent``. The user
 * sees this in their Notifications page route list, so we want
 * something more legible than the raw UA string.
 */
function deriveDeviceLabel(): string {
  const ua = navigator.userAgent || "";
  const browser = ua.match(/(Firefox|Edg|Chrome|Safari)\/[\d.]+/)?.[1] || "Browser";
  let platform = "Device";
  if (/iPhone|iPad|iOS/.test(ua)) platform = "iPhone/iPad";
  else if (/Android/.test(ua)) platform = "Android";
  else if (/Macintosh/.test(ua)) platform = "Mac";
  else if (/Windows/.test(ua)) platform = "Windows";
  else if (/Linux/.test(ua)) platform = "Linux";
  return `${browser} on ${platform}`;
}

export interface UseWebPushApi {
  /** Server-reported VAPID public key, or "" if not yet configured. */
  vapidPublicKey: string;
  /** True if the server has both halves of the VAPID keypair. */
  hasServerKeys: boolean;
  /** Routes belonging to the current user. */
  routes: PushRoute[];
  /** Only routes for the web_push backend. */
  webPushRoutes: PushRoute[];
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
  /**
   * Run the full subscribe flow: request Notification permission,
   * subscribe via the service worker, store the resulting subscription
   * as a route on the server. Returns the newly-created route.
   */
  subscribe: () => Promise<PushRoute>;
  /** Remove the route, then unsubscribe the browser. */
  unsubscribe: (routeId: string) => Promise<void>;
}

export function useWebPushApi(): UseWebPushApi {
  const { connected, rpc } = useWebSocket();
  const [vapidPublicKey, setVapidPublicKey] = useState<string>("");
  const [hasServerKeys, setHasServerKeys] = useState<boolean>(false);
  const [routes, setRoutes] = useState<PushRoute[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async (): Promise<void> => {
    if (!connected) return;
    setLoading(true);
    setError(null);
    try {
      const [backends, routesRes] = await Promise.all([
        rpc<PushBackendsListResult>({ type: "push.backends.list" }),
        rpc<PushRoutesListResult>({ type: "push.routes.list" }),
      ]);
      const wp: PushBackendEntry | undefined = (backends.backends || []).find(
        (b) => b.name === BACKEND_NAME,
      );
      setVapidPublicKey(String(wp?.runtime_data?.vapid_public_key || ""));
      setHasServerKeys(Boolean(wp?.runtime_data?.has_keys));
      setRoutes(routesRes.routes || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [connected, rpc]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const subscribe = useCallback(async (): Promise<PushRoute> => {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      throw new Error("This browser doesn't support Web Push.");
    }
    if (!vapidPublicKey) {
      throw new Error(
        "Server isn't configured with VAPID keys yet — ask an " +
          "admin to generate them under Settings → Notifications.",
      );
    }
    // Permission gate — must be triggered from a user gesture
    // (button click) per browser policy.
    const perm = await Notification.requestPermission();
    if (perm !== "granted") {
      throw new Error(
        "Browser notification permission was not granted.",
      );
    }
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: vapidPublicKeyToUint8(vapidPublicKey),
      });
    }
    const p256dh = arrayBufferToBase64Url(sub.getKey("p256dh"));
    const auth = arrayBufferToBase64Url(sub.getKey("auth"));
    const userAgent = deriveDeviceLabel();
    const result = await rpc<PushRoutesCreateResult>({
      type: "push.routes.create",
      backend_name: BACKEND_NAME,
      label: userAgent,
      destination_data: {
        endpoint: sub.endpoint,
        p256dh,
        auth,
        user_agent: userAgent,
      },
    });
    setRoutes((prev) => [...prev, result.route]);
    return result.route;
  }, [rpc, vapidPublicKey]);

  const unsubscribe = useCallback(
    async (routeId: string): Promise<void> => {
      await rpc<PushRoutesDeleteResult>({
        type: "push.routes.delete",
        route_id: routeId,
      });
      setRoutes((prev) => prev.filter((r) => r._id !== routeId));
      // Best-effort: drop the browser-side subscription too. Failing
      // here doesn't roll back the server-side delete — by design,
      // the route is the source of truth.
      try {
        if ("serviceWorker" in navigator) {
          const reg = await navigator.serviceWorker.ready;
          const sub = await reg.pushManager.getSubscription();
          if (sub) await sub.unsubscribe();
        }
      } catch {
        /* ignore */
      }
    },
    [rpc],
  );

  const webPushRoutes = useMemo(
    () => routes.filter((r) => r.backend_name === BACKEND_NAME),
    [routes],
  );

  return useMemo(
    () => ({
      vapidPublicKey,
      hasServerKeys,
      routes,
      webPushRoutes,
      loading,
      error,
      reload,
      subscribe,
      unsubscribe,
    }),
    [
      vapidPublicKey,
      hasServerKeys,
      routes,
      webPushRoutes,
      loading,
      error,
      reload,
      subscribe,
      unsubscribe,
    ],
  );
}
