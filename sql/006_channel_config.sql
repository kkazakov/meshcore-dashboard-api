-- 006_channel_config.sql
-- Creates the channel_config table for soft-delete and mute tracking.
-- Engine: ReplacingMergeTree (deduplicates by channel_name on merge).

CREATE TABLE IF NOT EXISTS channel_config
(
    channel_name String,
    muted_until  Nullable(DateTime64(3, 'UTC')),
    deleted      Bool DEFAULT false,
    updated_at   DateTime64(3, 'UTC') DEFAULT now64()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY channel_name;
