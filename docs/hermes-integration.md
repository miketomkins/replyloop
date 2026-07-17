# Hermes integration

ReplyLoop can be loaded as an optional Hermes plugin. The plugin keeps ReplyLoop local-first: the database stays in the ReplyLoop SQLite file, delivery is routed through Hermes' existing `send_message` tool, and gateway reply commands are handled only for exact direct-message matches.

## Install the package entry point

From this repository:

```bash
python3 -m pip install -e .
```

The package exposes this entry point:

```toml
[project.entry-points."hermes_agent.plugins"]
replyloop = "replyloop.hermes_plugin"
```

## Enable the plugin explicitly

Hermes plugins are opt-in. Enable `replyloop` in the Hermes profile that will run the gateway or scheduler:

```bash
hermes config set plugins.enabled '["replyloop"]'
```

Do not enable this against live personal channels until you have run the tests and reviewed the target placeholders below.

## Scheduler example

Run the scheduler as a no-agent job so Reminder delivery is deterministic and does not involve an LLM:

```bash
hermes cron create \
  --name replyloop-tick \
  --schedule 'every 1m' \
  --no-agent \
  --script '/path/to/replyloop-tick.sh'
```

Example script content, using placeholders only:

```bash
#!/usr/bin/env bash
set -euo pipefail
export REPLYLOOP_DB="/path/to/replyloop.db"
hermes replyloop --json tick
```

`hermes replyloop tick` uses the plugin bridge, which calls `ctx.dispatch_tool('send_message', {'action':'send', 'target':'<platform>:<chat>', 'message':'...'})`. If `send_message` returns missing or false `success`, ReplyLoop records a transport failure and keeps the occurrence pending for retry.

## Tools

The plugin registers these JSON tools:

- `replyloop_create`
- `replyloop_list`
- `replyloop_get`
- `replyloop_pause`
- `replyloop_resume`
- `replyloop_cancel`
- `replyloop_tick`
- `replyloop_doctor`

Tool handlers return JSON strings, catch exceptions, and redact target-like identifiers from errors.

## Photon/iMessage reply handling

The gateway hook runs before normal dispatch and only handles exact `DONE`, `SNOOZE`, or `CANCEL` text from exact Photon direct-message targets that match an open ReplyLoop occurrence. It rejects group traffic, other platforms, wrong senders, ambiguous matches, and unrelated text so those messages continue through the normal Hermes conversation path.

After a durable DB transition, the hook schedules a short acknowledgement through the live Photon adapter and returns `action=skip`, so no LLM turn is created. If the database mutation fails or the acknowledgement cannot be scheduled, the hook returns allow or no result rather than silently swallowing normal conversation.

## Security boundary

ReplyLoop never stores raw target identifiers in public CLI output, tool output, docs, or diagnostics. Use placeholder values such as `<platform>`, `<chat-id>`, and `<sender-id>` in docs and scripts. The Hermes source checkout is read-only evidence for compatibility tests; installing, enabling, restarting services, or sending through live adapters is a separate operator action.

## Live activation warning

This integration can send messages when enabled with a live Hermes gateway and scheduler. Before live activation, verify the database path, target placeholders, plugin allow-list, Photon configuration, and scheduler command in a non-personal test channel.
