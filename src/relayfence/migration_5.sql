CREATE TABLE member_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    member_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    paused_revision INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(project, member_id, request_id),
    FOREIGN KEY(project, task_id) REFERENCES tasks(project, task_id)
);
CREATE INDEX member_messages_history ON member_messages(project, member_id, id);
PRAGMA user_version=5;
