-- Channel registration store: any channel can self-register with Colony.
CREATE TABLE IF NOT EXISTS channels (
    channel_key    TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    gateway_family TEXT NOT NULL,
    manifest_json  TEXT NOT NULL,
    channel_token  TEXT NOT NULL,
    registered_at  TEXT NOT NULL,
    last_seen_at   TEXT,
    status         TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'inactive', 'revoked'))
);
