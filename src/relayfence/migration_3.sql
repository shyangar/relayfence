CREATE TABLE tasks_v3 (
    project TEXT NOT NULL, task_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
    inputs TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ready','running','done','paused','cancelled','failed')),
    revision INTEGER NOT NULL DEFAULT 1, fence INTEGER NOT NULL DEFAULT 0,
    owner TEXT, lease_hash TEXT, expires_at REAL, snapshot TEXT NOT NULL DEFAULT '[]',
    outputs TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL,
    role_id TEXT, execution TEXT, priority INTEGER NOT NULL DEFAULT 1,
    due_at REAL, depends_on TEXT NOT NULL DEFAULT '[]', task_snapshot TEXT NOT NULL DEFAULT '[]',
    failure TEXT, PRIMARY KEY(project,task_id)
);
INSERT INTO tasks_v3 (project,task_id,title,description,inputs,status,revision,fence,owner,lease_hash,expires_at,snapshot,outputs,created_at,updated_at,role_id,execution)
SELECT project,task_id,title,description,inputs,status,revision,fence,owner,lease_hash,expires_at,snapshot,outputs,created_at,updated_at,role_id,execution FROM tasks;
DROP TABLE tasks;
ALTER TABLE tasks_v3 RENAME TO tasks;
CREATE INDEX tasks_project_status ON tasks(project,status,task_id);
CREATE TABLE runners (
    project TEXT NOT NULL, agent_id TEXT NOT NULL, runner_id TEXT NOT NULL,
    active_task TEXT, updated_at REAL NOT NULL, PRIMARY KEY(project,agent_id)
);
PRAGMA user_version = 3;
