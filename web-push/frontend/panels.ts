/**
 * Side-effect import: register the web-push plugin's account-page
 * subscribe panel.
 *
 * ``web_push.subscribe`` matches the backend's
 * ``WebPushPlugin.ui_panels()`` declaration.
 */

import { registerPanel } from "@/lib/plugin-panels";

import { SubscribePanel } from "./SubscribePanel";

registerPanel("web_push.subscribe", SubscribePanel);
