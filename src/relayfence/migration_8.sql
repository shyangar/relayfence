CREATE TABLE IF NOT EXISTS agent_credentials (
    software TEXT NOT NULL,
    provider TEXT NOT NULL,
    name TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    hint TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (software, provider, name)
);
PRAGMA user_version=8;
