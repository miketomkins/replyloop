ALTER TABLE delivery_attempts ADD COLUMN logical_delivery_id TEXT;
ALTER TABLE delivery_attempts ADD COLUMN applied_to_occurrence INTEGER NOT NULL DEFAULT 0;

UPDATE delivery_attempts
SET logical_delivery_id = 'replyloop:' || occurrence_id || ':delivery:' || (
    SELECT COUNT(*)
    FROM delivery_attempts earlier
    WHERE earlier.occurrence_id = delivery_attempts.occurrence_id
      AND earlier.status = 'success'
      AND (earlier.attempted_at < delivery_attempts.attempted_at
           OR (earlier.attempted_at = delivery_attempts.attempted_at AND earlier.id <= delivery_attempts.id))
)
WHERE status = 'success';

UPDATE delivery_attempts
SET logical_delivery_id = 'replyloop:' || occurrence_id || ':delivery:' || (
    1 + (
        SELECT COUNT(*)
        FROM delivery_attempts earlier
        WHERE earlier.occurrence_id = delivery_attempts.occurrence_id
          AND earlier.status = 'success'
          AND (earlier.attempted_at < delivery_attempts.attempted_at
               OR (earlier.attempted_at = delivery_attempts.attempted_at AND earlier.id < delivery_attempts.id))
    )
)
WHERE status = 'failure';

UPDATE delivery_attempts SET applied_to_occurrence = 1 WHERE status = 'success';

CREATE INDEX idx_delivery_attempts_logical_delivery ON delivery_attempts(occurrence_id, logical_delivery_id);
CREATE INDEX idx_delivery_attempts_applied_success ON delivery_attempts(occurrence_id, status, applied_to_occurrence, attempted_at);
