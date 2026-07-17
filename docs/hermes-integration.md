# Hermes integration

ReplyLoop can be loaded as an optional Hermes plugin. The plugin keeps ReplyLoop local-first: the database stays in the ReplyLoop SQLite file, delivery is routed through Hermes transport, and gateway reply commands are handled only for exact direct-message matches.

## Package entry point

The package exposes this optional plugin entry point:

```toml
[project.entry-points."hermes_agent.plugins"]
replyloop = "replyloop.hermes_plugin"
```

Installing ReplyLoop does not enable live messaging. Hermes plugin activation, scheduler setup, and channel configuration are separate operator actions.

## Enable explicitly

Hermes plugins are opt-in. Enable `replyloop` only in the Hermes profile that will run a reviewed test gateway or scheduler:

```bash
hermes plugins enable replyloop
```

Do not enable this against live personal channels until placeholders, database path, scheduler cadence, and transport behavior have been reviewed in an operator-controlled test channel.

## Scheduler example

Run the scheduler as a no-agent job so reminder delivery is deterministic and does not involve an LLM:

```bash
hermes cron create \
  --name replyloop-tick \
  'every 1m' \
  --no-agent \
  --script 'replyloop-tick.sh'
```

Example script content, using placeholders only:

```bash
#!/usr/bin/env bash
set -euo pipefail
export REPLYLOOP_DB="/path/to/replyloop.db"
hermes replyloop --json tick >/dev/null
```

The stdout redirection is intentional for `--no-agent` cron jobs: Hermes delivers any non-empty script stdout verbatim. Successful no-op ticks should stay silent while stderr and nonzero exit status still surface failures.

The `--script` value is relative to the operator's Hermes scripts directory. The command above is an example only and should not be run until placeholders are replaced in an operator-controlled environment.

## Tools

The plugin registers JSON tools for create, list, get, pause, resume, cancel, tick, and doctor operations. Create requires user-visible `title` and `message` fields, stores them on the reminder, and list/get return both fields. Tool handlers return JSON strings, catch exceptions, and redact target-like identifiers from errors.

## Delivery bridge

Hermes delivery builds a platform target from the stored ReplyLoop target and sends the stored title/message plus due time and supported commands through Hermes `send_message`. Supported commands are `DONE`, `SNOOZE`, `SNOOZE <duration>`, and `CANCEL`. If the Hermes transport reports missing or false success, ReplyLoop records a failure and keeps the occurrence pending for retry.

## Photon reply handling

The gateway hook runs before normal dispatch and only handles exact `DONE`, `SNOOZE`, `SNOOZE <duration>`, or `CANCEL` text from exact Photon direct-message targets that match an open ReplyLoop occurrence. It rejects group traffic, other platforms, wrong senders, ambiguous matches, and unrelated text so those messages continue through normal Hermes conversation handling.

After a durable database transition, the hook schedules a short acknowledgement through the live Photon adapter and returns `action=skip`. Plugin registration installs a narrowly scoped logging filter on Hermes gateway logs so ReplyLoop handled-skip records render a one-way chat label instead of a raw chat identifier. It does not mutate the shared gateway event object, so later plugins still receive the original routing identity.

If the privacy guard is unavailable, the hook is not registered. If the database mutation fails or acknowledgement cannot be scheduled, the hook returns allow or no result rather than silently swallowing normal conversation.

## Security boundary

ReplyLoop never stores raw target identifiers in public CLI output, tool output, docs, or diagnostics. Use placeholder values such as `<platform>`, `<chat-id>`, and `<sender-id>` in docs and scripts. Installing, enabling, restarting services, or sending through live adapters is outside this public hardening task.
