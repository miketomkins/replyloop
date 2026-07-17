ALTER TABLE occurrences ADD COLUMN delivery_claim_id TEXT;

CREATE INDEX idx_occurrences_claim_id ON occurrences(delivery_claim_id);
