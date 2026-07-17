# Operations

ReplyLoop is local-first. Operational commands use only the configured SQLite database and never delete reminder history during normal create, delivery, reply, backup, restore, or diagnostic flows.

## Database location

The standalone CLI chooses its database path in this order:

1. `--db PATH`
2. `REPLYLOOP_DB`
3. `$XDG_DATA_HOME/replyloop/replyloop.db`
4. The user's local data directory under `replyloop/replyloop.db`

No database path is stored in Git. Use environment variables or scheduler configuration to choose a deployment path.

## Creating reminders

Use one schedule mode per create command:

```sh
replyloop create --id demo --title "Daily check-in" --message "Send the project update." --daily --time 09:00 --timezone UTC --platform synthetic --chat example-chat --sender example-sender
replyloop create --title "One-time check-in" --message "Send the launch note." --once-at 2026-01-01T09:00:00Z --timezone UTC --platform synthetic --chat example-chat --sender example-sender
replyloop create --title "Weekly review" --message "Review open loops." --weekly --weekday 0 --weekday 4 --time 17:30 --timezone UTC --platform synthetic --chat example-chat --sender example-sender
```

`--target` also accepts an equivalent JSON object when values are synthetic placeholders. Public create commands require `--title` and `--message`; ReplyLoop stores those fields and uses them for every delivery.

Validation errors name the field or flag combination to fix and avoid printing stored targets.

## Scheduler

Run the tick command from cron, systemd timers, launchd, Hermes cron, or another scheduler:

```sh
REPLYLOOP_DB=/var/lib/replyloop/state.db replyloop tick --json
```

`tick` creates due occurrences, uses the configured delivery adapter, and exits nonzero when any due delivery fails. With `--json`, delivery records are collected under the final `deliveries` array so stdout is one valid JSON document. Failed deliveries remain in the retry queue so later ticks can retry according to the service retry policy.

For local deterministic testing you can force a transport failure:

```sh
replyloop tick --fail --json
```

## Replies

Use `reply` to test lifecycle handling without a network adapter:

```sh
replyloop reply --platform synthetic --chat example-chat --sender example-sender --chat-type dm DONE
replyloop reply --platform synthetic --chat example-chat --sender example-sender --chat-type dm "SNOOZE 30m"
replyloop reply --platform synthetic --chat example-chat --sender example-sender --chat-type dm CANCEL
```

Group traffic is ignored by reply handling. Wrong senders, wrong chats, unrelated text, and ambiguous latest matches do not mutate state.

## Backup

The backup command uses SQLite's online backup API. It writes to a temporary file in the destination directory, verifies the temporary database by reopening it read-only and running `PRAGMA integrity_check`, then atomically replaces the requested destination.

```sh
replyloop backup /var/backups/replyloop/state.db
```

A successful command prints `integrity_check: ok`. A failed command leaves the existing destination untouched when the filesystem supports atomic replace. The destination must not be the live database file or any live SQLite sidecar for that database (`-wal`, `-shm`, or `-journal`), including hardlinks or normalized path aliases.

## Restore

1. Stop schedulers or workers that may write to the database.
2. Copy the current database file aside as a safety snapshot.
3. Copy the verified backup to the configured database path.
4. Run `replyloop doctor --json`.
5. Restart the scheduler.

Restore does not require migrations to be run manually. The CLI opens the database through the normal connection path and applies pending migrations if future versions add them.

## Doctor

`replyloop doctor --json` checks schema version, SQLite `quick_check`, parent directory accessibility, due and pending counts, retry queue count, and clock/timezone readiness. Doctor output intentionally omits reminder targets. The command exits nonzero when any diagnostic check fails, including corrupt or unreadable SQLite files.

## No data deletion guarantee

Pause, resume, cancel, done, snooze, tick, reply, doctor, backup, and restore are projection updates or file-copy operations. They do not purge reminders, occurrences, delivery attempts, or events. If a future maintenance command adds deletion or compaction, it must be explicit, documented separately, and covered by tests.
