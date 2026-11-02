CREATE TABLE IF NOT EXISTS decisions (
 project TEXT NOT NULL, decision_id TEXT NOT NULL, meeting_id TEXT,
 task_id TEXT, kind TEXT NOT NULL, question TEXT NOT NULL,
 status TEXT NOT NULL, revision INTEGER NOT NULL, task_revision INTEGER,
 answer TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(project,decision_id)
);
CREATE INDEX IF NOT EXISTS decision_pending ON decisions(project,status,meeting_id,task_id);
INSERT INTO decisions(project,decision_id,meeting_id,task_id,kind,question,status,revision,created_at,updated_at)
SELECT project,'legacy-gate-' || meeting_id,meeting_id,NULL,'execution_gate',
 '{"question":"讨论已达成一致，下一步如何处理？","context":"你可以保存方案结束讨论，或确认后进入执行。","options":["仅保存方案","确认并执行","补充要求后重新讨论"]}',
 'pending',1,json_extract(definition,'$.created_at'),json_extract(definition,'$.created_at')
FROM meetings m WHERE json_extract(definition,'$.stage')='ready_for_execution'
AND NOT EXISTS(SELECT 1 FROM decisions d WHERE d.project=m.project AND d.meeting_id=m.meeting_id AND d.kind='execution_gate');
PRAGMA user_version=11;
