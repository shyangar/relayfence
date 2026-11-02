CREATE TABLE office_configs (
    project TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    definition TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE quota_observations (
    project TEXT NOT NULL,
    route_id TEXT NOT NULL,
    observation TEXT NOT NULL,
    observed_at REAL NOT NULL,
    PRIMARY KEY (project, route_id)
);
PRAGMA user_version=4;
