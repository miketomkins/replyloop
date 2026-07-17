# Privacy and security

ReplyLoop is designed for local-first operation and public-source hygiene. Runtime data belongs in local databases, logs, and configuration files that are not committed to Git.

## Repository safety rules

Public files must not contain:

- Personal reminders or message transcripts
- Phone numbers, chat IDs, sender IDs, or real account identifiers
- Credentials, tokens, private keys, cookies, or auth files
- Local database files, backups, or logs
- Private hostnames or private network addresses
- Machine-specific absolute paths

Documentation and tests must use synthetic examples. Use `example.com` and reserved documentation networks such as `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, and `2001:db8::/32` when examples need domains or addresses.

## Local data model

ReplyLoop stores reminder state, occurrence state, immutable delivery attempts, and append-only domain events in SQLite. Current-state columns are projections for efficient queries, not replacements for event history.

## Transport and escalation

Transport retry handles delivery failure before a user receives a reminder. Escalation is user-visible repetition after a successful delivery remains unresolved. These clocks are separate so outages do not masquerade as delivered reminders.

External exactly-once delivery depends on adapter or provider support for `DeliveryRequest.idempotency_key`. Local transactions and claim fencing prevent local double-application, but cannot deduplicate a provider that ignores the key after a timeout or crash following a successful send.

## Hermes integration

Hermes is optional. When enabled, Hermes platform adapters supply messaging transport while ReplyLoop owns scheduling, state transitions, retries, escalation, reply matching, and audit history. Plugin tools and diagnostics must avoid returning credentials, raw target identifiers, or internal exception traces.

## Public audit

Run the repository audit before commits and in CI:

```bash
python3 scripts/public_repo_audit.py .
```

The audit inspects tracked files, untracked non-ignored files, symlink targets, and Git history. It reports file and line evidence without printing candidate secret values. It is a defense-in-depth guardrail for common privacy mistakes and known token shapes, not proof that no possible secret exists. Use it alongside human review and platform secret scanning before publishing changes.
