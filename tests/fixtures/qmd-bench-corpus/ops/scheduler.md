# Scheduler Backstop

Observational Memory uses launchd LaunchAgents instead of cron by default on macOS.
Cron remains the fallback on other Unix-like systems or when a user explicitly prefers it.

The Codex observer cadence is controlled by `OM_CODEX_OBSERVER_INTERVAL_MINUTES` in `~/.config/observational-memory/env`.
That setting lets maintainers speed up or slow down the polling backstop without changing hook behavior.
