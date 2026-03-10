-- 00_init.sql
-- Runs automatically when the ClickHouse container starts for the first time.
-- Creates the database, then all application tables.

CREATE DATABASE IF NOT EXISTS meshcore_dashboard;

-- Switch to the application database for all subsequent statements
USE meshcore_dashboard;

-- 001_authentication: users table
CREATE TABLE IF NOT EXISTS users
(
    email         String,
    password_hash String,
    username      String,
    active        Bool                 DEFAULT true,
    access_rights String               DEFAULT '',
    updated_at    DateTime64(3, 'UTC') DEFAULT now64()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY email;

-- Seed: admin@example.com / admin
INSERT INTO users (email, password_hash, username, active, access_rights)
SELECT
    'admin@example.com',
    '$2b$12$anP.RAuPjeyuo.QEyYiZouOICisaPk/KZ1ge5DhxF45uF8G08Rh5q',
    'admin',
    true,
    ''
WHERE NOT EXISTS (
    SELECT 1 FROM users WHERE email = 'admin@example.com'
);

-- 002_messages: all received MeshCore messages
CREATE TABLE IF NOT EXISTS messages
(
    received_at          DateTime64(3, 'UTC')  DEFAULT now64(),
    msg_type             LowCardinality(String),
    channel_idx          Int8                  DEFAULT -1,
    channel_name         String                DEFAULT '',
    sender_timestamp     UInt32                DEFAULT 0,
    sender_pubkey_prefix String                DEFAULT '',
    sender_name          String                DEFAULT '',
    path_len             UInt8                 DEFAULT 0,
    snr                  Float32               DEFAULT 0,
    text                 String,
    txt_type             UInt8                 DEFAULT 0,
    signature            String                DEFAULT ''
)
ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(received_at)
ORDER BY (msg_type, channel_idx, sender_timestamp, sender_pubkey_prefix, text)
SETTINGS index_granularity = 8192;

-- 003_repeaters: repeaters to monitor for telemetry
CREATE TABLE IF NOT EXISTS repeaters
(
    id         UUID                 DEFAULT generateUUIDv4(),
    name       String,
    public_key String,
    password   String               DEFAULT '',
    enabled    Bool                 DEFAULT true,
    created_at DateTime64(3, 'UTC') DEFAULT now64()
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (id);

-- 004_repeater_telemetry: metric time-series from monitored repeaters
CREATE TABLE IF NOT EXISTS repeater_telemetry
(
    recorded_at   DateTime64(3, 'UTC')  DEFAULT now64(),
    repeater_id   UUID,
    repeater_name String,
    metric_key    LowCardinality(String),
    metric_value  Float64
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(recorded_at)
ORDER BY (repeater_id, recorded_at, metric_key)
SETTINGS index_granularity = 8192;

-- 005_tokens: session tokens
CREATE TABLE IF NOT EXISTS tokens
(
    token      String,
    email      String,
    created_at DateTime64(3, 'UTC') DEFAULT now64(),
    expires_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY token
TTL expires_at DELETE;
