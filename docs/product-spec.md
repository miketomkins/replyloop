# ReplyLoop Product Specification

ReplyLoop is a local-first commitment reminder engine. It creates scheduled reminder occurrences, records delivery attempts, retries failed transports, escalates unanswered delivered reminders, and resolves exact user replies.

## MVP capabilities

- Create reminders with `once`, `daily`, or `weekly` schedules in an IANA timezone.
- Allow daily and weekly schedules to include multiple local `HH:MM` times.
- Store per-reminder escalation intervals and a default snooze duration.
- Create due occurrences idempotently during scheduler ticks.
- Treat delivery retry separately from user-visible escalation.
- Mark a reminder delivered only after a transport success.
- Keep failed deliveries retryable without consuming escalation steps.
- Resolve exact `DONE`, `SNOOZE`, `SNOOZE <duration>`, and `CANCEL` commands for the matching direct-message target.
- Preserve append-only event history and immutable delivery attempts.
- Provide backup, doctor, and migration mechanisms for the local SQLite store.
- Keep Hermes integration optional through platform-neutral adapters.

## Reply commands

Supported commands are case-insensitive and whitespace-normalized:

- `DONE`
- `SNOOZE`
- `SNOOZE 30m`
- `SNOOZE 2h`
- `SNOOZE 1d`
- `CANCEL`

Bare commands resolve the most recently delivered unresolved occurrence for the exact direct-message target. If no matching occurrence exists, ReplyLoop does not mutate data or consume unrelated conversation.

## Schedules

Schedule examples use synthetic data only:

```json
{"kind":"once","at":"2026-07-20T09:00:00-07:00"}
```

```json
{"kind":"daily","times":["08:00","14:00","20:00"]}
```

```json
{"kind":"weekly","weekdays":[0,2,4],"times":["09:00"]}
```

Weekdays use Monday `0` through Sunday `6`. Invalid, duplicated, or empty schedule elements are rejected before storage. Daylight-saving behavior follows Python `zoneinfo` and must be covered by deterministic tests.

## Non-goals for v0.1

- Web dashboard
- Native mobile application
- Shared or multi-user tenancy
- Group-chat command handling
- Natural-language reply interpretation
- Medical, legal, or safety-critical advice
- Direct ownership of messaging credentials
- Calendar synchronization
- Arbitrary cron expression parsing
- Deleting reminder or event history through message commands
