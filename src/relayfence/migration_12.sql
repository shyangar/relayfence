CREATE TABLE IF NOT EXISTS task_policies (
 project TEXT NOT NULL, task_id TEXT NOT NULL,
 review_required INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(project,task_id)
);
CREATE TABLE IF NOT EXISTS task_reviews (
 project TEXT NOT NULL, task_id TEXT NOT NULL, fence INTEGER NOT NULL,
 status TEXT NOT NULL, reviewer TEXT, note TEXT NOT NULL DEFAULT '',
 evidence TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL,
 PRIMARY KEY(project,task_id,fence)
);
CREATE INDEX IF NOT EXISTS events_task_history ON events(project,subject,seq);
PRAGMA user_version = 12;
