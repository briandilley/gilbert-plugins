# Non-pip OS deps go through `runtime_dependencies()` + `doctor`, and checks must exercise the dep

A plugin that needs OS-level prerequisites beyond what `pyproject.toml` can install (Chromium,
tesseract, ffmpeg, system fonts) declares them as `RuntimeDependency` entries via
`Plugin.runtime_dependencies()`. `./gilbert.sh doctor` runs every plugin's checks and reports
PASS/FAIL with install hints. A check must **actually exercise** the dependency (e.g. launch headless
Chromium), not just probe a file path — a path probe passed while Playwright's headless launch
failed for missing OS shared libs, which is what motivated this rule.

## Consequences

`doctor --install` only auto-runs `auto_install_cmd` for opt-in, unattended-safe installs (e.g. a
per-user browser cache). Anything needing `sudo`/`apt-get`/Docker stays manual.
