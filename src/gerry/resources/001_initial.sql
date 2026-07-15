CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS data_snapshots (
  id uuid PRIMARY KEY,
  election_id text NOT NULL,
  effective_date date NOT NULL,
  status text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS source_artifacts (
  id bigserial PRIMARY KEY,
  snapshot_id uuid NOT NULL REFERENCES data_snapshots(id) ON DELETE CASCADE,
  source text NOT NULL, url text, local_path text NOT NULL, sha256 char(64) NOT NULL
);
CREATE TABLE IF NOT EXISTS precincts (
  key text NOT NULL,
  snapshot_id uuid NOT NULL REFERENCES data_snapshots(id) ON DELETE CASCADE,
  teryt char(6) NOT NULL, number integer NOT NULL, special boolean NOT NULL DEFAULT false,
  population integer, eligible integer NOT NULL DEFAULT 0, votes jsonb NOT NULL DEFAULT '{}',
  quality text NOT NULL, reconstruction jsonb NOT NULL DEFAULT '{}',
  geometry geometry(MultiPolygon,2180),
  PRIMARY KEY(snapshot_id,key)
);
CREATE INDEX IF NOT EXISTS precincts_geom_gix ON precincts USING gist(geometry);
CREATE INDEX IF NOT EXISTS precincts_snapshot_teryt_idx ON precincts(snapshot_id,teryt);
CREATE TABLE IF NOT EXISTS adjacency_edges (
  id bigserial PRIMARY KEY,
  snapshot_id uuid NOT NULL REFERENCES data_snapshots(id) ON DELETE CASCADE,
  source text NOT NULL,
  target text NOT NULL,
  shared_border_m double precision NOT NULL, kind text NOT NULL DEFAULT 'physical',
  CHECK(source < target), UNIQUE(snapshot_id,source,target,kind),
  CONSTRAINT adjacency_edges_snapshot_source_fkey
    FOREIGN KEY(snapshot_id,source) REFERENCES precincts(snapshot_id,key) ON DELETE CASCADE,
  CONSTRAINT adjacency_edges_snapshot_target_fkey
    FOREIGN KEY(snapshot_id,target) REFERENCES precincts(snapshot_id,key) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS optimization_runs (
  id uuid PRIMARY KEY, status text NOT NULL, request jsonb NOT NULL, result jsonb,
  certificate_path text, certificate_verified boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
