# ReplyLoop

ReplyLoop is a local-first commitment reminder engine for reminders that need deterministic follow-through. It stores schedules, delivery attempts, replies, retries, escalation, and audit history locally so a reminder can survive restarts and temporary transport failures.

ReplyLoop is designed around explicit commands instead of natural-language guessing:

- `DONE` completes the most recent unresolved reminder occurrence for the exact direct-message target.
- `SNOOZE` defers an occurrence by the reminder default.
- `SNOOZE <duration>` can override the default with values such as `30m`, `2h`, or `1d`.
- `CANCEL` disables the reminder while preserving history.

Hermes Agent integration is optional. When used with Hermes, iMessage support is supplied through Hermes platform adapters; ReplyLoop remains responsible for schedules, occurrence state, retries, escalation, and local audit history.

## Privacy posture

This public repository must contain only synthetic examples. Do not commit personal reminders, phone numbers, chat IDs, sender IDs, credentials, local database files, logs, private hostnames, or machine-specific paths.

## Status

Stage 0 baseline: documentation, contribution policy, security policy, ignore rules, and a public repository audit tool. Runtime implementation comes later.
