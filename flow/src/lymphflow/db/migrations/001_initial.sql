-- lymphflow schema v1.
-- Arrays (events, gate masks, job outputs) live in files; SQLite holds only metadata.

CREATE TABLE users (
  id          TEXT PRIMARY KEY,
  created_at  TEXT NOT NULL
);

CREATE TABLE experiments (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  owner_id    TEXT NOT NULL REFERENCES users(id),
  revision    INTEGER NOT NULL DEFAULT 0,      -- bumped on every gating-template change
  created_at  TEXT NOT NULL
);

-- Compensation matrices are immutable and content-addressed.
CREATE TABLE comp_matrices (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  hash        TEXT NOT NULL UNIQUE,
  detectors   TEXT NOT NULL,                   -- JSON list of PnN labels
  matrix      TEXT NOT NULL,                   -- JSON row-major n x n
  source      TEXT NOT NULL                    -- 'embedded' | 'imported'
);

CREATE TABLE samples (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  sha256        TEXT NOT NULL,                 -- of the uploaded FCS file
  fcs_version   TEXT,
  n_events      INTEGER NOT NULL,
  n_params      INTEGER NOT NULL,
  n_nonfinite   INTEGER NOT NULL DEFAULT 0,    -- events with any NaN/inf value
  parquet_file  TEXT NOT NULL,                 -- relative to events_dir
  dtype         TEXT NOT NULL,                 -- 'float32' | 'float64'
  -- Compensation state. 'embedded': valid matrix found in the file; 'none': no matrix;
  -- 'identity': matrix present but identity; 'invalid': matrix names detectors the file lacks.
  comp_state    TEXT NOT NULL,
  comp_matrix_id INTEGER REFERENCES comp_matrices(id),
  comp_enabled  INTEGER NOT NULL DEFAULT 1,    -- user switch; meaningful only with a matrix
  comp_message  TEXT,
  group_label   TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL
);
CREATE INDEX samples_experiment ON samples(experiment_id);

CREATE TABLE channels (
  sample_id   INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
  idx         INTEGER NOT NULL,                -- 0-based parameter index
  col         TEXT NOT NULL,                   -- Parquet column name (p001 ...)
  pnn         TEXT NOT NULL,
  pns         TEXT NOT NULL DEFAULT '',
  kind        TEXT NOT NULL,                   -- 'scatter' | 'time' | 'fluoro'
  pnr         REAL,
  vmin        REAL,
  vmax        REAL,
  PRIMARY KEY (sample_id, idx)
);

-- Only allow-listed FCS TEXT keywords are ever stored (see core/deid.py).
CREATE TABLE sample_keywords (
  sample_id   INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
  key         TEXT NOT NULL,
  value       TEXT NOT NULL,
  PRIMARY KEY (sample_id, key)
);

-- Scales are immutable and content-addressed. A gate dimension references a scale row,
-- never "the channel's current scale", so editing a display scale cannot move a gate.
CREATE TABLE scales (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  hash        TEXT NOT NULL UNIQUE,
  kind        TEXT NOT NULL,
  params      TEXT NOT NULL                    -- JSON
);

-- Current display scale per channel (by PnN) within an experiment.
CREATE TABLE channel_scales (
  experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
  pnn           TEXT NOT NULL,
  scale_id      INTEGER NOT NULL REFERENCES scales(id),
  PRIMARY KEY (experiment_id, pnn)
);

-- The gating template of an experiment: a tree of gates.
CREATE TABLE gates (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
  parent_id     INTEGER REFERENCES gates(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL,                 -- 'rect' | 'range' | 'polygon' | 'ellipse'
  dims          TEXT NOT NULL,                 -- JSON [{pnn, scale_id, comp}]
  geom          TEXT NOT NULL,                 -- JSON, coordinates in the dims' scale units
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX gates_experiment ON gates(experiment_id);

-- Per-sample geometry override of a template gate (same kind and dimensions).
CREATE TABLE gate_overrides (
  gate_id     INTEGER NOT NULL REFERENCES gates(id) ON DELETE CASCADE,
  sample_id   INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
  geom        TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (gate_id, sample_id)
);

-- Statistics are keyed by the gate's content hash for that sample, so a row computed for
-- an older geometry / scale / compensation can never be shown for the current one.
CREATE TABLE population_stats (
  sample_id   INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
  gate_id     INTEGER NOT NULL,                -- 0 = root (all events)
  gate_key    TEXT NOT NULL,
  stats       TEXT NOT NULL,                   -- JSON
  computed_at TEXT NOT NULL,
  PRIMARY KEY (sample_id, gate_id)
);

CREATE TABLE jobs (
  id            TEXT PRIMARY KEY,
  experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
  type          TEXT NOT NULL,
  state         TEXT NOT NULL,                 -- queued | running | done | failed | cancelled
  owner_id      TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  finished_at   TEXT,
  integrated    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX jobs_experiment ON jobs(experiment_id);

CREATE TABLE audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  action      TEXT NOT NULL,
  detail      TEXT NOT NULL DEFAULT '{}'
);
