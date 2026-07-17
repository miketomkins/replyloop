# Security Policy

## Supported version

ReplyLoop v0.1.0 is the current supported public baseline. It includes the local SQLite engine, standalone CLI, optional Hermes plugin, public repository audit, and CI verification.

## Responsible disclosure

Report suspected vulnerabilities privately through the repository security advisory workflow when available. If that is unavailable, open a minimal issue requesting a private contact path. Do not include exploit details, credentials, personal data, phone numbers, chat IDs, sender IDs, database files, logs, screenshots containing identifiers, or local paths in public reports.

## Public issue safety

Use synthetic fixtures only. Public issues, pull requests, commits, docs, and attachments must not contain personal reminders, message transcripts, live target identifiers, credentials, local database files, logs, backups, private hostnames, private network addresses, or machine-specific paths.

## Security model

ReplyLoop is local-first. Reminder state, occurrence state, immutable delivery attempts, and append-only events live in an operator-controlled SQLite database. The repository audit and CI guard against common public-source leaks, but they do not replace human review or platform secret scanning.

Hermes integration is optional. When enabled, Hermes supplies messaging transport and gateway events; ReplyLoop owns schedule evaluation, state transitions, retry/escalation accounting, reply matching, and audit history. Live activation against personal channels is an operator decision outside this public hardening release.
