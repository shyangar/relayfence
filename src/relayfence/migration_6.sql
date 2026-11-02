CREATE TABLE meetings (
 project TEXT NOT NULL, meeting_id TEXT NOT NULL, definition TEXT NOT NULL,
 PRIMARY KEY(project, meeting_id)
);
CREATE TABLE meeting_tasks (
 project TEXT NOT NULL, task_id TEXT NOT NULL, meeting_id TEXT NOT NULL,
 member_id TEXT NOT NULL, group_id TEXT NOT NULL, stage TEXT NOT NULL,
 PRIMARY KEY(project, task_id),
 FOREIGN KEY(project, task_id) REFERENCES tasks(project, task_id)
);
PRAGMA user_version=6;
