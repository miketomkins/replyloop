# Changelog

## v0.1.0 - 2026-07-17

Initial public hardening release.

### Added
- Local-first SQLite reminder engine with migrations, event history, delivery attempts, backup, and doctor diagnostics.
- `replyloop` CLI for create, list, show, pause, resume, cancel, tick, reply, backup, and doctor workflows.
- Once, daily, and weekly schedules with IANA timezones, multiple local times, deterministic DST handling, and per-reminder snooze and escalation policy.
- Exact `DONE`, `SNOOZE`, `SNOOZE <duration>`, and `CANCEL` reply semantics for direct-message targets.
- Optional Hermes plugin entry point with scheduler tools, gateway reply hook, Photon delivery adapter, and privacy-preserving skip logging.
- Public repository audit covering tracked files, untracked files, Git history, credential markers, private paths, local artifacts, private networks, and real-looking messaging identifiers.
- GitHub Actions CI for Python 3.11, 3.12, and 3.13.

### Security
- Runtime dependencies are empty for the standalone package.
- Public examples use synthetic placeholders and reserved documentation values only.
- Tool and diagnostic output avoids raw target identifiers and credential-like values.
