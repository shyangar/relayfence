CREATE TABLE IF NOT EXISTS items (
    project TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('state','artifact')),
    version INTEGER NOT NULL CHECK(version > 0), PRIMARY KEY(project,key)
);
CREATE TABLE IF NOT EXISTS revisions (
    project TEXT NOT NULL, key TEXT NOT NULL, version INTEGER NOT NULL,
    value TEXT NOT NULL, dependencies TEXT NOT NULL, actor TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY(project,key,version), FOREIGN KEY(project,key) REFERENCES items(project,key)
);
CREATE TABLE IF NOT EXISTS tasks (
    project TEXT NOT NULL, task_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
    inputs TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ready','running','done')),
    revision INTEGER NOT NULL DEFAULT 1, fence INTEGER NOT NULL DEFAULT 0,
    owner TEXT, lease_hash TEXT, expires_at REAL, snapshot TEXT NOT NULL DEFAULT '[]',
    outputs TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY(project,task_id)
);
CREATE TABLE IF NOT EXISTS receipts (
    project TEXT NOT NULL, actor TEXT NOT NULL, request_id TEXT NOT NULL,
    digest TEXT NOT NULL, response TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY(project,actor,request_id)
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, actor TEXT NOT NULL,
    kind TEXT NOT NULL, subject TEXT NOT NULL, details TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_project_seq ON events(project,seq);
PRAGMA user_version = 1;
