# Product behavior

ReplyLoop creates scheduled reminder occurrences with user-supplied title and message content, records delivery attempts, retries failed transports, escalates unanswered delivered reminders, and resolves exact user replies.

## MVP capabilities

- Create reminders with `once`, `daily`, or `weekly` schedules in an IANA timezone.
- Allow daily and weekly schedules to include multiple unique local `HH:MM` times.
- Store per-reminder escalation intervals, maximum delivery count, repeat-last policy, and default snooze duration.
- Create due occurrences idempotently during scheduler ticks.
- Treat delivery retry separately from user-visible escalation.
- Mark an occurrence delivered only after a transport success.
- Keep failed deliveries retryable without consuming escalation steps.
- Resolve exact `DONE`, `SNOOZE`, `SNOOZE <duration>`, and `CANCEL` commands for the matching direct-message target.
- Render deliveries from stored title and message content, followed by due time and supported reply commands.
- Ignore wrong senders, wrong chats, group traffic, unrelated text, and ambiguous latest matches.
- Preserve append-only event history and immutable delivery attempts.
- Provide backup, doctor, and migration mechanisms for the local SQLite store.
- Keep Hermes integration optional through platform-neutral adapters.

## Reminder content contract

Public creation surfaces require user-supplied `title` and `message` values. Stored deliveries render that exact content, then append due time and supported reply commands.

Both values must be strings and must contain non-whitespace characters after trimming. ReplyLoop normalizes leading and trailing whitespace once at creation, stores and returns the normalized `title` and `message`, and renders those stored values without an additional content transform.

The internal Python `ReminderService.create_reminder` API retains source-compatible defaults only for existing in-process callers: omitted `title` becomes `Reminder`, and omitted `message` becomes `Reminder is due.`. Those deterministic compatibility defaults are not a public user contract; CLI and Hermes tool callers must provide explicit content.

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

```json
{"kind":"once","at":"2026-07-20T09:00:00Z"}
{"kind":"daily","times":["08:00","14:00","20:00"]}
{"kind":"weekly","weekdays":[0,2,4],"times":["09:00"]}
```

Weekdays use Monday `0` through Sunday `6`. Invalid, duplicated, empty, ambiguous, or timezone-inconsistent schedule elements are rejected before storage where applicable.

DST behavior is deterministic: nonexistent local wall times during spring-forward gaps are skipped; ambiguous local wall times during fall-back folds produce both UTC instants.

## Non-goals for v0.1.0

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
