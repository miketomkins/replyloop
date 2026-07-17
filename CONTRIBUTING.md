# Contributing

ReplyLoop is privacy-first. Contributions must keep the public repository safe for cloning, indexing, and CI logs.

## Rules

- Use synthetic fixtures only.
- Do not commit personal reminders, phone numbers, chat IDs, sender IDs, credentials, local databases, backups, logs, private hostnames, or machine-specific paths.
- Do not paste real message transcripts into tests or docs.
- Keep Hermes integration optional and adapter-based.
- Run the public repository audit and tests before submitting changes.

## Verification

```bash
python3 scripts/public_repo_audit.py .
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

If a test needs sensitive-looking input, construct it dynamically in the test so the repository source itself remains clean.
