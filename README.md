# ReplyLoop

ReplyLoop is a local-first commitment reminder engine for reminders that need deterministic follow-through. It stores schedules, delivery attempts, replies, retries, escalation, and audit history in a local SQLite database so reminder state survives restarts and temporary transport failures.

## Problem statement

Simple reminder bots usually guess from free-form chat, lose state during transport outages, or cannot explain what happened after a missed reminder. ReplyLoop takes the opposite path: explicit schedules, explicit reply commands, local durable state, and auditable transitions.

## Quick start

```bash
python3 -m pip install .
export REPLYLOOP_DB="$(mktemp -d)/replyloop.db"
replyloop --json doctor
replyloop --json create --id demo --daily --time 09:00 --timezone UTC --platform synthetic --chat example-chat --sender example-sender
replyloop --json tick
replyloop --json reply --platform synthetic --chat example-chat --sender example-sender --chat-type dm DONE
```

The standalone `tick` command uses a deterministic stdout adapter. Optional live delivery is provided by adapters such as Hermes when explicitly enabled by an operator.

## Per-reminder schedules

Every reminder has one schedule, one IANA timezone, one target, a default snooze duration, and optional escalation intervals.

Supported schedules:

```json
{"kind":"once","at":"2026-07-20T09:00:00Z"}
{"kind":"daily","times":["08:00","14:00","20:00"]}
{"kind":"weekly","weekdays":[0,2,4],"times":["09:00"]}
```

Weekly days use Monday `0` through Sunday `6`. Daily and weekly schedules may include multiple unique `HH:MM` local wall-clock times. Nonexistent DST spring-forward wall times are skipped. Ambiguous fall-back wall times emit both matching UTC instants.

Escalation is per reminder:

```bash
replyloop create --id demo --daily --time 09:00 --timezone UTC --platform synthetic --chat example-chat --sender example-sender --snooze 30 --escalation 10 --escalation 60 --max-deliveries 3
```

Transport retry is separate from escalation. A failed transport does not count as a delivered user-visible reminder.

## Command semantics

Reply commands are exact, case-insensitive, and whitespace-normalized:

- `DONE`: completes the latest unresolved delivered occurrence for the exact direct-message target.
- `SNOOZE`: defers that occurrence by the reminder default snooze duration.
- `SNOOZE <duration>`: defers by an explicit duration such as `30m`, `2h`, or `1d`.
- `CANCEL`: cancels the reminder and any open due, delivered, snoozed, or in-flight occurrences.

Groups are ignored by reply handling. Wrong senders, wrong chats, unrelated text, and ambiguous latest matches do not mutate state.

## CLI reference

- `create`: create a reminder from `--once-at`, `--daily`, `--weekly`, or `--schedule-json`.
- `list`: list reminders, optionally filtered by status.
- `show`: show one reminder and its occurrences.
- `pause`: pause an active reminder.
- `resume`: resume a paused reminder.
- `cancel`: terminally cancel a reminder.
- `tick`: create due occurrences and attempt delivery.
- `reply`: process a deterministic local reply command.
- `backup`: create an integrity-checked SQLite backup.
- `doctor`: run database, migration, clock, timezone, due-count, pending-count, and retry-queue diagnostics.

Database path resolution order: `--db`, `REPLYLOOP_DB`, `$XDG_DATA_HOME/replyloop/replyloop.db`, then the user's local data directory.

## Architecture

ReplyLoop is a small Python package with no runtime dependencies. SQLite stores reminders, occurrences, immutable delivery attempts, and append-only events. Schedule generation uses Python `zoneinfo`. Delivery adapters receive a `DeliveryRequest` with an idempotency key and return a structured `DeliveryOutcome`. State changes are fenced by SQLite transactions and occurrence claim IDs.

See `docs/architecture.md` for the detailed module and data model.

## Privacy model

The public repository must contain only synthetic examples. Do not commit personal reminders, phone numbers, chat IDs, sender IDs, credentials, local databases, logs, backups, private hostnames, private network addresses, or machine-specific paths.

`python3 scripts/public_repo_audit.py .` scans repository files and Git history for common credential shapes, local artifacts, private paths, private networks, and real-looking messaging identifiers. It reports file and line evidence without printing candidate secret values.

## Optional Hermes integration

ReplyLoop can be loaded as an optional Hermes plugin through the `hermes_agent.plugins` entry point. Hermes supplies messaging transport and gateway events; ReplyLoop keeps local scheduling, occurrence state, retry/escalation accounting, reply matching, and audit history. Live activation is not part of installation and must be done explicitly by an operator.

See `docs/hermes-integration.md` for plugin behavior and activation cautions.

## Limitations

- No web dashboard.
- No native mobile app.
- No shared tenancy or multi-user permissions model.
- No natural-language reply interpretation.
- No group-chat command handling.
- No calendar sync or arbitrary cron parser.
- External exactly-once delivery depends on the adapter/provider honoring the idempotency key after a send timeout or crash.

## Roadmap

- Operator-friendly adapter examples for non-personal test channels.
- Exportable operational reports from event history.
- Optional dashboard over local read-only state.
- Additional transport adapters with explicit idempotency guarantees.
