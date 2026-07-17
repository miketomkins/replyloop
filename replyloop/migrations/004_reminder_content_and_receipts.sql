ALTER TABLE reminders ADD COLUMN title TEXT NOT NULL DEFAULT 'Reminder';
ALTER TABLE reminders ADD COLUMN message TEXT NOT NULL DEFAULT 'Reminder is due.';
ALTER TABLE delivery_attempts ADD COLUMN provider_message_id TEXT;

UPDATE reminders
SET title = 'Reminder ' || id
WHERE title = 'Reminder' OR trim(title) = '';

UPDATE reminders
SET message = 'Reminder ' || id || ' is due.'
WHERE message = 'Reminder is due.' OR trim(message) = '';
