# Architecture

ReplyLoop is a local-first Python package built around deterministic state transitions and a small SQLite schema.

## Components

- `replyloop.cli`: standalone command line interface and JSON output boundary.
- `replyloop.schedules`: schedule validation and due-time generation for `once`, `daily`, and `weekly` schedules.
- `replyloop.db`: SQLite connection management, migrations, transactions, current-state projections, and event writes.
- `replyloop.service`: reminder lifecycle orchestration, occurrence creation, delivery claims, retry, escalation, and reply handling.
- `replyloop.delivery`: adapter protocol, delivery requests, delivery outcomes, and synthetic test adapter.
- `replyloop.replies`: exact reply parsing, duration parsing, and target matching.
- `replyloop.hermes_plugin`: optional Hermes tools, delivery bridge, and gateway hook.
- `scripts/public_repo_audit.py`: public-source privacy and secret audit.

## Data model

SQLite tables are created by package migrations:

- `schema_migrations`: applied migration versions.
- `reminders`: target, schedule JSON, timezone, status, default snooze, escalation policy, timestamps.
- `occurrences`: deterministic occurrence IDs, scheduled time, due time, status, delivery claim, timestamps.
- `delivery_attempts`: immutable transport attempts, logical delivery IDs, status, transport, error, applied flag, timestamps.
- `events`: append-only domain events for reminder and occurrence transitions.

The current-state tables make operational queries efficient. The event table provides a durable audit trail.

## Tick lifecycle

1. Recover stale delivery claims whose lease expired.
2. Generate due occurrences for active reminders between the last scheduled occurrence and the current clock time.
3. Queue eligible escalations for delivered occurrences whose escalation interval has elapsed.
4. List due or snoozed occurrences.
5. Claim each occurrence with a fresh claim ID.
6. Call the delivery adapter with a logical idempotency key.
7. Record success or failure in a transaction fenced by occurrence status and claim ID.

Duplicate ticks and racing workers converge through deterministic occurrence IDs, unique database constraints, status filters, and claim fencing.

## Reply lifecycle

1. Parse only exact `DONE`, `SNOOZE`, `SNOOZE <duration>`, or `CANCEL` commands.
2. Reject group traffic.
3. Find the latest delivered unresolved occurrence whose stored target exactly matches the incoming platform, chat, sender, and direct-message flag.
4. If exactly one match exists, mutate occurrence or reminder state and append events in one transaction.
5. If no match or multiple equally latest matches exist, do nothing.

## DST and timezones

Schedules use IANA timezone names loaded through Python `zoneinfo`. Due-time generation treats schedules as local wall-clock commitments. Spring-forward nonexistent wall times are skipped. Fall-back ambiguous wall times generate both corresponding UTC instants.

## Packaging

`pyproject.toml` uses setuptools and keeps runtime dependencies empty. Package data includes SQL migrations. The `replyloop` console script maps to `replyloop.cli:main`, and the optional Hermes plugin entry point maps to `replyloop.hermes_plugin`.

## Failure boundaries

- Transport failures are recorded as failed delivery attempts and retried later.
- Database write failures abort the current transaction.
- A delivery outcome that cannot be recorded restores the local claim where possible and raises for operator visibility.
- Corrupt or unreadable databases cause `doctor` to return a failed diagnostic instead of exposing raw target data.
