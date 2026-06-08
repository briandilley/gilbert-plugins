# Plugins ship their own frontend, auto-discovered via `import.meta.glob`

A plugin keeps all of its TS/TSX under `<plugin>/frontend/` and registers components by `panel_id`
in a side-effect `panels.ts`. Core's Vite build picks every `<plugin>/frontend/panels.ts` up
automatically through an `import.meta.glob`. Core never imports from a plugin's frontend, and a
backend-only load (plugin without its frontend bundle) silently skips the unregistered panels.

Adding a plugin's UI is therefore purely additive — no edits to core. The coupling between a backend
`UIPanel`/`UIRoute` declaration and its SPA component is a single string `panel_id`; a typo or a
missing registration just makes the panel silently absent rather than erroring.
