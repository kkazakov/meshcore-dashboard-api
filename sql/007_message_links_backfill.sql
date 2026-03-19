-- Backfill existing messages into message_links table
-- Run this ONCE after creating the table and materialized view

INSERT INTO message_links
SELECT
    received_at,
    sender_name AS username,
    channel_name,
    link
FROM messages
ARRAY JOIN extractAll(text, 'https://[^\s"\'<>]+') AS link
WHERE text LIKE '%https://%';
