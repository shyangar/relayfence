CREATE TABLE IF NOT EXISTS local_projects (
    project TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS group_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    group_id TEXT NOT NULL,
    member_id TEXT,
    task_id TEXT,
    kind TEXT NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('debug','info','warning','error')),
    message TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS group_logs_page ON group_logs(project, group_id, id);
CREATE INDEX IF NOT EXISTS group_logs_task ON group_logs(project, task_id, id);
CREATE INDEX IF NOT EXISTS group_logs_latest ON group_logs(project, id);
INSERT INTO group_logs (
    project, group_id, member_id, task_id, kind, level, message, details, created_at
)
SELECT
    e.project,
    mt.group_id,
    mt.member_id,
    e.subject,
    e.kind,
    CASE
        WHEN e.kind = 'task.failed' THEN 'error'
        WHEN e.kind IN ('task.pause', 'task.cancel', 'task.released') THEN 'warning'
        ELSE 'info'
    END,
    CASE e.kind
        WHEN 'task.created' THEN '任务已创建'
        WHEN 'task.updated' THEN '任务说明已更新'
        WHEN 'task.assigned' THEN '任务已分派'
        WHEN 'task.claimed' THEN '任务开始执行'
        WHEN 'task.released' THEN '执行已释放'
        WHEN 'task.completed' THEN '任务已经完成'
        WHEN 'task.failed' THEN '任务执行失败'
        WHEN 'task.pause' THEN '任务已暂停'
        WHEN 'task.resume' THEN '任务已恢复排队'
        WHEN 'task.cancel' THEN '任务已取消'
        WHEN 'task.retry' THEN '任务已重新排队'
        WHEN 'task.reopened' THEN '任务已重新打开'
        WHEN 'office.transferred' THEN '任务已交接'
        WHEN 'office.handoff' THEN '任务已安全交接'
        ELSE e.kind
    END,
    e.details,
    e.created_at
FROM events e
JOIN meeting_tasks mt ON mt.project = e.project AND mt.task_id = e.subject
WHERE e.kind LIKE 'task.%'
  AND e.kind NOT IN ('task.guidance', 'task.heartbeat', 'runner.heartbeat')
ORDER BY e.seq;
PRAGMA user_version=7;
