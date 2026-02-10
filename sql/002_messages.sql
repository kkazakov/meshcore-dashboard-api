-- 002_messages.sql
-- Stores all received MeshCore messages (channel and direct/private).
--
-- Message types
-- -------------
--   msg_type = 'CHAN'  : broadcast channel message
--   msg_type = 'PRIV' : direct (private) message between contacts
--
-- Sender identification
-- ---------------------
--   Channel messages carry no sender public key — only the channel index and
--   name are known.  Direct messages carry a 6-byte pubkey_prefix that
--   identifies the sender contact.
--
-- Deduplication key
-- -----------------
--   (sender_timestamp, msg_type, channel_idx, sender_pubkey_prefix, text)
--   ReplacingMergeTree deduplicates on the ORDER BY key at merge time; the
--   received_at column is kept for tie-breaking / audit purposes.

CREATE TABLE IF NOT EXISTS messages
(
    -- When the row was ingested by this server (server-side clock, UTC)
    received_at          DateTime64(3, 'UTC')  DEFAULT now64(),

    -- Message type: 'CHAN' (channel broadcast) or 'PRIV' (direct/private)
    msg_type             LowCardinality(String),

    -- ── Channel fields (msg_type = 'CHAN') ──────────────────────────────────
    -- Slot index on the companion device (0-7); -1 when not applicable
    channel_idx          Int8                  DEFAULT -1,
    -- Human-readable channel name resolved at ingest time (empty for PRIV)
    channel_name         String                DEFAULT '',

    -- ── Sender fields ───────────────────────────────────────────────────────
    -- Unix timestamp reported by the sender device (seconds since epoch)
    sender_timestamp     UInt32                DEFAULT 0,
    -- 6-byte public-key prefix identifying the sender (hex); empty for CHAN
    sender_pubkey_prefix String                DEFAULT '',
    -- Human-readable contact name resolved from the contacts list at ingest time
    sender_name          String                DEFAULT '',

    -- ── Routing / radio fields ───────────────────────────────────────────────
    -- Number of hops the message traveled (0 = direct)
    path_len             UInt8                 DEFAULT 0,
    -- Signal-to-noise ratio at the receiving device (dB, v3+ only; 0 if unknown)
    snr                  Float32               DEFAULT 0,

    -- ── Message content ─────────────────────────────────────────────────────
    -- Raw text payload
    text                 String,
    -- Text type flag from the protocol (0 = plain, 1 = ?, 2 = signed, …)
    txt_type             UInt8                 DEFAULT 0,
    -- Signature bytes (hex, 4 bytes); non-empty only when txt_type = 2 (PRIV)
    signature            String                DEFAULT ''
)
ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(received_at)
ORDER BY (msg_type, channel_idx, sender_timestamp, sender_pubkey_prefix, text)
SETTINGS index_granularity = 8192;
