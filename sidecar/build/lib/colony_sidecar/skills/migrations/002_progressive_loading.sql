-- Migration 002: progressive skill loading columns
-- Safe to run on existing databases; columns added only if absent.

ALTER TABLE skills ADD COLUMN trigger_patterns TEXT NOT NULL DEFAULT '[]';
ALTER TABLE skills ADD COLUMN context_tokens_estimate INTEGER NOT NULL DEFAULT 2048;
ALTER TABLE skills ADD COLUMN lazy_loader TEXT;
