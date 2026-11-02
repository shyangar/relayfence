CREATE TABLE IF NOT EXISTS office_snapshots (
    project TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    label TEXT NOT NULL,
    definition TEXT NOT NULL,
    group_count INTEGER NOT NULL,
    member_count INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (project, snapshot_id)
);
CREATE INDEX IF NOT EXISTS office_snapshots_project ON office_snapshots(project, created_at);
PRAGMA user_version=9;
