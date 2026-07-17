# ReplyLoop operations

ReplyLoop is local-first. Operational commands use only the configured SQLite database and never delete reminder history as part of normal create, delivery, reply, backup, restore, or diagnostic flows.

## Database location

The standalone CLI chooses its database path in this order:

1. `--db PATH`
2. `REPLYLOOP_DB`
3. `$XDG_DATA_HOME/replyloop/replyloop.db`
4. `~/.local/share/replyloop/replyloop.db`

No database path is stored in Git. Use environment variables or scheduler configuration to choose a deployment path.

## Creating reminders

Use one schedule mode per create command:

```sh
replyloop create --id water --daily --time 09:00 --timezone UTC --platform telegram --chat c1
replyloop create --once-at 2026-01-01T09:00:00Z --timezone UTC --platform telegram --chat c1
replyloop create --weekly --weekday 0 --weekday 4 --time 17:30 --timezone UTC --platform telegram --chat c1
replyloop create --schedule-json '{"kind":"daily","times":["09:00"]}' --timezone UTC --target '{"platform":"telegram","chat_id":"c1"}'
```

Validation errors name the field or flag combination to fix and do not print stored targets.

## Scheduler

Run the tick command from cron, systemd timers, launchd, or another scheduler:

```sh
REPLYLOOP_DB=/var/lib/replyloop/state.db replyloop tick --json
```

`tick` creates due occurrences, uses a deterministic stdout delivery adapter, and exits nonzero when any due delivery fails. With `--json`, delivery records are collected under the final `deliveries` array so stdout is one valid JSON document. Without `--json`, delivery records may also be printed as they are attempted. Failed deliveries remain in the retry queue so later ticks can retry according to the service retry policy.

For local deterministic testing you can force a transport failure:

```sh
replyloop tick --fail --json
```

## Replies

Use `reply` to test lifecycle handling without a network adapter:

```sh
replyloop reply --platform telegram --chat c1 --sender s1 --chat-type dm DONE
replyloop reply --platform telegram --chat c1 --sender s1 --chat-type dm "SNOOZE 30m"
replyloop reply --platform telegram --chat c1 --sender s1 --chat-type dm CANCEL
```

Group traffic is ignored unless the original target was a group target.

## Backup

The backup command uses SQLite's online backup API. It writes to a temporary file in the destination directory, verifies the temporary database by reopening it read-only and running `PRAGMA integrity_check`, then atomically replaces the requested destination.

```sh
replyloop backup /var/backups/replyloop/state.db
```

A successful command prints `integrity_check: ok`. A failed command leaves the existing destination untouched when the filesystem supports atomic replace.

## Restore

1. Stop schedulers or workers that may write to the database.
2. Copy the current database file aside as a safety snapshot.
3. Copy the verified backup to the configured database path.
4. Run `replyloop doctor --json`.
5. Restart the scheduler.

Restore does not require migrations to be run manually. The CLI opens the database through the normal connection path and applies pending migrations if future versions add them.

## Doctor

`replyloop doctor --json` checks:

- schema version
- SQLite `quick_check`
- parent directory readability, writability, and search permission
- due and pending counts
- retry queue count
- clock and timezone readiness

Doctor output intentionally omits reminder targets. Counts are safe for operational dashboards.
The command exits nonzero when any diagnostic check fails, including corrupt or unreadable SQLite files. The `retry_queue` count includes only currently outstanding retry work, not historical failures that later succeeded or were closed.

## Status changes

Lifecycle commands enforce explicit transitions:

- `pause`: active to paused
- `resume`: paused to active
- `cancel`: active or paused to cancelled

Cancelled reminders are terminal and cannot be resumed. Cancelling a reminder also cancels open delivered, due, snoozed, or in-flight occurrences so reminder and occurrence projections stay consistent.

## Troubleshooting

- `database does not exist`: create a reminder first or point `REPLYLOOP_DB` at the intended state file.
- `choose exactly one schedule mode`: pass only one of `--schedule-json`, `--once-at`, `--daily`, or `--weekly`.
- `--daily requires at least one --time HH:MM`: provide one or more local wall-clock times.
- `unknown timezone`: use an IANA timezone available to Python `zoneinfo`, such as `UTC`.
- `cannot resume reminder in cancelled status`: cancellation is terminal; create a new reminder instead.
- `quick_check` or `integrity_check` is not `ok`: stop schedulers, preserve the database file, restore the newest verified backup, and investigate storage health before restarting.

## No data deletion guarantee

Pause, resume, cancel, done, snooze, tick, reply, doctor, backup, and restore guidance are projection updates or file-copy operations. They do not purge reminders, occurrences, delivery attempts, or events. If a future maintenance command adds deletion or compaction, it must be explicit, documented separately, and covered by tests.
