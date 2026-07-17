CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE reminders (
    id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    schedule_json TEXT NOT NULL,
    timezone TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'cancelled')),
    default_snooze_minutes INTEGER NOT NULL CHECK (default_snooze_minutes > 0),
    escalation_minutes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_reminders_status_target ON reminders(status, target);

CREATE TABLE occurrences (
    id TEXT PRIMARY KEY,
    reminder_id TEXT NOT NULL REFERENCES reminders(id) ON DELETE RESTRICT,
    scheduled_for TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('due', 'delivering', 'delivered', 'done', 'snoozed', 'cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (reminder_id, scheduled_for)
);

CREATE INDEX idx_occurrences_due_status ON occurrences(due_at, status);
CREATE INDEX idx_occurrences_reminder_status ON occurrences(reminder_id, status);

CREATE TABLE delivery_attempts (
    id TEXT PRIMARY KEY,
    occurrence_id TEXT NOT NULL REFERENCES occurrences(id) ON DELETE RESTRICT,
    attempted_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'failure')),
    transport TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_delivery_attempts_occurrence ON delivery_attempts(occurrence_id, attempted_at);
CREATE INDEX idx_delivery_attempts_status ON delivery_attempts(status, attempted_at);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_events_aggregate ON events(aggregate_type, aggregate_id, id);
CREATE INDEX idx_events_type_time ON events(event_type, created_at);
