-- stock_setup_state lifecycle timestamps must come from snapshot/replay time.
-- Run once on the trades database after deploying the matching Python patch.
-- This removes MySQL wall-clock defaults and ON UPDATE behavior so omitted
-- timestamps fail instead of silently corrupting lifecycle chronology.

ALTER TABLE stock_setup_state
    MODIFY COLUMN created_at DATETIME NOT NULL,
    MODIFY COLUMN updated_at DATETIME NOT NULL;
