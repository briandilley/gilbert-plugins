/**
 * Side-effect import: register the mafia plugin's full-page UI.
 * Panel id mirrors plugin.py MafiaPlugin.ui_routes() — mounted at /mafia.
 */

import { registerPanel } from "@/lib/plugin-panels";

import { MafiaPage } from "./MafiaPage";

registerPanel("mafia.page", MafiaPage);
