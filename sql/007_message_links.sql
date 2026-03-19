-- 007_message_links: extracted HTTPS links from messages
-- Populated via materialized view from messages table

CREATE TABLE IF NOT EXISTS message_links
(
    received_at    DateTime64(3, 'UTC'),
    username       String,
    channel_name   String,
    link           String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(received_at)
ORDER BY (received_at, username, channel_name, link)
SETTINGS index_granularity = 8192;

-- Materialized view to extract HTTPS links from incoming messages
CREATE MATERIALIZED VIEW IF NOT EXISTS message_links_mv
TO message_links
AS SELECT
    received_at,
    sender_name AS username,
    channel_name,
    link
FROM messages
ARRAY JOIN extractAll(text, 'https://[^\s"\'<>]+') AS link
WHERE text LIKE '%https://%';
