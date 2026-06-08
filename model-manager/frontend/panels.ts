/**
 * Side-effect import: register the model-manager plugin's full-page UI.
 *
 * This file is the only place the SPA needs to know about the model-manager
 * plugin. It's pulled in by ``frontend/src/plugins/index.ts`` (imported once
 * from ``main.tsx``) so the registration lands before any page mounts.
 */

import { registerPanel } from "@/lib/plugin-panels";
import { ModelManagerPage } from "./ModelManagerPage";

// Panel id mirrors plugin.py ``ModelManagerPlugin.ui_routes()`` — the SPA
// mounts <ModelManagerPage /> at the route's path (/models).
registerPanel("model-manager.page", ModelManagerPage);
