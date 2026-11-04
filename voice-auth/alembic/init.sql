-- Voice Authentication Database Schema
-- PostgreSQL + pgvector for voiceprint embedding storage

CREATE EXTENSION IF NOT EXISTS vector;

-- Users table: maps caller identity to voiceprint enrollment status
CREATE TABLE IF NOT EXISTS voice_users (
    user_id          TEXT PRIMARY KEY,
    display_name     TEXT,
    phone_number     TEXT,
    enrolled         BOOLEAN NOT NULL DEFAULT FALSE,
    enrolled_at      TIMESTAMPTZ,
    enrollment_count INTEGER NOT NULL DEFAULT 0,
    -- Template adaptation: store a rolling average embedding
    -- updated on each successful verification
    last_verified_at TIMESTAMPTZ,
    verify_count     INTEGER NOT NULL DEFAULT 0,
    reject_count     INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Voiceprints: ECAPA-TDNN embeddings (192-dim) stored as pgvector
CREATE TABLE IF NOT EXISTS voiceprints (
    id              SERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES voice_users(user_id) ON DELETE CASCADE,
    embedding       vector(192) NOT NULL,
    audio_duration  REAL,
    -- Track which enrollment session this came from
    session_type    TEXT NOT NULL DEFAULT 'enrollment', -- enrollment | adaptation
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for fast user lookup
CREATE INDEX IF NOT EXISTS idx_voiceprints_user_id ON voiceprints(user_id);
CREATE INDEX IF NOT EXISTS idx_voiceprints_created ON voiceprints(created_at DESC);

-- Verification audit log: every verification attempt for FAR/FRR monitoring
CREATE TABLE IF NOT EXISTS verification_log (
    id              SERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES voice_users(user_id) ON DELETE CASCADE,
    score           REAL NOT NULL,
    threshold       REAL NOT NULL,
    decision        TEXT NOT NULL CHECK (decision IN ('accept', 'reject')),
    -- Anti-spoofing result
    spoof_score     REAL,
    spoof_decision  TEXT CHECK (spoof_decision IN ('bonafide', 'spoof', 'unknown')),
    audio_duration  REAL,
    -- Whether passphrase was also checked (second factor)
    passphrase_ok   BOOLEAN,
    final_decision  TEXT NOT NULL CHECK (final_decision IN ('authenticated', 'rejected', 'pending')),
    call_id         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_verif_log_user ON verification_log(user_id);
CREATE INDEX IF NOT EXISTS idx_verif_log_created ON verification_log(created_at DESC);

-- Enrollment sessions: track enrollment calls
CREATE TABLE IF NOT EXISTS enrollment_sessions (
    id              SERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES voice_users(user_id) ON DELETE CASCADE,
    call_id         TEXT,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'failed')),
    audio_duration  REAL,
    sample_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);