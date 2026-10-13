CREATE TABLE agents (
    project TEXT NOT NULL, agent_id TEXT NOT NULL, revision INTEGER NOT NULL,
    definition TEXT NOT NULL, token_hash TEXT UNIQUE, updated_at REAL NOT NULL,
    PRIMARY KEY(project,agent_id)
);
CREATE TABLE roles (
    project TEXT NOT NULL, role_id TEXT NOT NULL, revision INTEGER NOT NULL,
    definition TEXT NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY(project,role_id)
);
ALTER TABLE tasks ADD COLUMN role_id TEXT;
ALTER TABLE tasks ADD COLUMN execution TEXT;
PRAGMA user_version = 2;
