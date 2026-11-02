CREATE INDEX IF NOT EXISTS events_project_subject_seq ON events(project, subject, seq);
CREATE INDEX IF NOT EXISTS meeting_tasks_member ON meeting_tasks(project, member_id, task_id);
CREATE INDEX IF NOT EXISTS member_messages_member_time ON member_messages(project, member_id, created_at, id);
PRAGMA user_version=10;
