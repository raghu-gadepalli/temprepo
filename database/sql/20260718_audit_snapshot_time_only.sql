-- Audit timestamp cleanup
--
-- auditlog.ts is the only authoritative timestamp and must be supplied from
-- the source snapshot/event. Remove wall-clock insertion/archive metadata so
-- replay and live exports expose one time system only.

ALTER TABLE auditlog
    MODIFY COLUMN ts DATETIME NOT NULL,
    DROP COLUMN created_at;

ALTER TABLE auditlog_history
    DROP COLUMN archived_at,
    DROP COLUMN created_at;
