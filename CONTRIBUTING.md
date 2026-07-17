# Contributing

ReplyLoop is privacy-first. Contributions must keep the public repository safe for cloning, indexing, and CI logs.

## Rules

- Use synthetic fixtures only.
- Do not commit personal reminders, phone numbers, chat IDs, sender IDs, credentials, local databases, backups, logs, private hostnames, private network addresses, or machine-specific paths.
- Do not paste real message transcripts into tests, docs, issues, commits, or pull requests.
- Keep runtime dependencies empty unless a release explicitly changes that constraint.
- Keep Hermes integration optional and adapter-based.
- Preserve deterministic command semantics for `DONE`, `SNOOZE`, `SNOOZE <duration>`, and `CANCEL`.
- Add or update tests for behavior changes.

## Verification

```bash
python3 scripts/public_repo_audit.py .
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q replyloop
```

For release hardening, also build a wheel, install only that wheel into a disposable virtual environment, run `replyloop --json doctor`, and run the same audit and tests from a clean local clone.

If a test needs sensitive-looking input, construct it dynamically in the test so repository source stays clean.
