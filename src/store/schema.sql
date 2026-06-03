-- Tollgate storage schema. See CLAUDE.md §6.6.
-- Privacy default (§9): metadata + content hash only. raw_logged defaults to 0.

CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY,
  ts INTEGER NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  route_label TEXT,
  input_tokens_est INTEGER,
  input_tokens_actual INTEGER,
  output_tokens_actual INTEGER,
  est_input_cost REAL,
  est_output_cost REAL,
  upstream_ms INTEGER,
  content_hash TEXT,
  raw_logged INTEGER NOT NULL DEFAULT 0,
  request_type TEXT
);

CREATE TABLE IF NOT EXISTS findings (
  request_id TEXT NOT NULL REFERENCES requests(id),
  rule TEXT NOT NULL,
  severity TEXT NOT NULL,
  tokens_wasted_est INTEGER NOT NULL,
  message TEXT NOT NULL,
  location_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
