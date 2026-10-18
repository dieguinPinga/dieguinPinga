-- Revisiones de proyecto: cada revisión es una misión nueva vinculada a su proyecto.
-- Los INTENTOS siguen dentro de cada misión (reparación); las REVISIONES continúan un proyecto terminado
-- con nuevas instrucciones humanas. Las versiones anteriores no se modifican nunca.
ALTER TABLE missions ADD COLUMN project_id TEXT;
ALTER TABLE missions ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE missions ADD COLUMN parent_id TEXT;
ALTER TABLE missions ADD COLUMN base_ref TEXT;
ALTER TABLE missions ADD COLUMN instruction TEXT;
CREATE INDEX missions_project ON missions(project_id, revision);

CREATE TABLE projects (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    current_mission TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE project_events (
    id          INTEGER PRIMARY KEY,
    project_id  TEXT NOT NULL,
    mission_id  TEXT,
    kind        TEXT NOT NULL,
    detail      TEXT,
    username    TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX project_events_project ON project_events(project_id, id);
