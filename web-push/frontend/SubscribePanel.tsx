/**
 * Per-user account panel: "Enable browser notifications".
 *
 * Mounted into the ``account.extensions`` slot via
 * ``WebPushPlugin.ui_panels()``. The button kicks off the full
 * subscribe flow:
 *
 *  1. Request ``Notification.permission`` (must be a user gesture).
 *  2. Resolve ``navigator.serviceWorker.ready`` — relies on a service
 *     worker the parent agent registers in the core SPA bundle.
 *  3. ``pushManager.subscribe({applicationServerKey: VAPID public})``.
 *  4. POST the resulting ``PushSubscription`` to the server as a
 *     ``push.routes.create`` RPC with ``backend_name="web_push"``.
 *
 * If the user has already subscribed this device, we surface the
 * route and offer an Unsubscribe button. The user can have multiple
 * devices subscribed at once — each browser produces its own route.
 */

import { useMemo, useState } from "react";

import { useWebPushApi } from "./api";

export function SubscribePanel() {
  const {
    vapidPublicKey,
    hasServerKeys,
    webPushRoutes,
    loading,
    error,
    subscribe,
    unsubscribe,
  } = useWebPushApi();

  const [busy, setBusy] = useState<boolean>(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const supported = useMemo(() => {
    if (typeof window === "undefined") return false;
    return (
      "serviceWorker" in navigator &&
      "PushManager" in window &&
      "Notification" in window
    );
  }, []);

  const permission = useMemo<NotificationPermission | "unknown">(() => {
    if (typeof Notification === "undefined") return "unknown";
    return Notification.permission;
  }, []);

  const handleSubscribe = async () => {
    setBusy(true);
    setActionError(null);
    setInfo(null);
    try {
      const route = await subscribe();
      setInfo(`Subscribed as "${route.label}".`);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleUnsubscribe = async (routeId: string) => {
    setBusy(true);
    setActionError(null);
    setInfo(null);
    try {
      await unsubscribe(routeId);
      setInfo("Unsubscribed.");
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4 p-4 border rounded">
      <div>
        <h3 className="text-lg font-medium">Browser notifications</h3>
        <p className="text-sm text-muted-foreground">
          Get Gilbert push notifications on this browser, even when
          the tab is closed.
        </p>
      </div>

      {!supported && (
        <div className="p-3 bg-muted rounded text-sm">
          This browser doesn't support Web Push. Try a recent Chrome,
          Edge, Firefox, or Safari 16.4+ on iOS (PWA must be added to
          Home Screen first on iOS).
        </div>
      )}

      {supported && permission === "denied" && (
        <div className="p-3 bg-yellow-50 border border-yellow-200 rounded text-sm">
          You previously blocked notifications for this site. Re-enable
          them in your browser's site settings, then come back here
          and click Subscribe.
        </div>
      )}

      {supported && !hasServerKeys && !loading && (
        <div className="p-3 bg-yellow-50 border border-yellow-200 rounded text-sm">
          The server hasn't generated VAPID keys yet. Ask an admin to
          visit Settings → Notifications → Web Push and click
          "Generate VAPID keys".
        </div>
      )}

      {error && (
        <div className="p-3 bg-red-50 border border-red-200 rounded text-sm">
          {error}
        </div>
      )}

      {actionError && (
        <div className="p-3 bg-red-50 border border-red-200 rounded text-sm">
          {actionError}
        </div>
      )}

      {info && (
        <div className="p-3 bg-green-50 border border-green-200 rounded text-sm">
          {info}
        </div>
      )}

      {webPushRoutes.length === 0 ? (
        <div className="space-y-2">
          <p className="text-sm">Not subscribed on this device.</p>
          <button
            type="button"
            onClick={handleSubscribe}
            disabled={
              busy ||
              !supported ||
              permission === "denied" ||
              !hasServerKeys ||
              !vapidPublicKey
            }
            className="px-3 py-2 bg-primary text-primary-foreground rounded disabled:opacity-50"
          >
            {busy ? "Subscribing..." : "Enable browser notifications"}
          </button>
        </div>
      ) : (
        <div className="space-y-2">
          <p className="text-sm">Subscribed devices:</p>
          <ul className="space-y-1">
            {webPushRoutes.map((r) => (
              <li
                key={r._id}
                className="flex items-center justify-between text-sm border rounded px-2 py-1"
              >
                <span>{r.label || "Unnamed device"}</span>
                <button
                  type="button"
                  onClick={() => handleUnsubscribe(r._id)}
                  disabled={busy}
                  className="text-xs underline disabled:opacity-50"
                >
                  Unsubscribe
                </button>
              </li>
            ))}
          </ul>
          <button
            type="button"
            onClick={handleSubscribe}
            disabled={
              busy ||
              !supported ||
              permission === "denied" ||
              !hasServerKeys ||
              !vapidPublicKey
            }
            className="text-xs underline disabled:opacity-50"
          >
            Subscribe this device too
          </button>
        </div>
      )}
    </div>
  );
}
