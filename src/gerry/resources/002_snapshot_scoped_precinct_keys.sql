-- Upgrade installations created by 001_initial.sql before snapshot-scoped keys.
-- Reapplying is safe: constraints are dropped and recreated in one transaction.
ALTER TABLE adjacency_edges DROP CONSTRAINT IF EXISTS adjacency_edges_source_fkey;
ALTER TABLE adjacency_edges DROP CONSTRAINT IF EXISTS adjacency_edges_target_fkey;
ALTER TABLE adjacency_edges DROP CONSTRAINT IF EXISTS adjacency_edges_snapshot_source_fkey;
ALTER TABLE adjacency_edges DROP CONSTRAINT IF EXISTS adjacency_edges_snapshot_target_fkey;

ALTER TABLE precincts DROP CONSTRAINT IF EXISTS precincts_pkey;
ALTER TABLE precincts ADD CONSTRAINT precincts_pkey PRIMARY KEY (snapshot_id, key);

ALTER TABLE adjacency_edges
  ADD CONSTRAINT adjacency_edges_snapshot_source_fkey
  FOREIGN KEY (snapshot_id, source)
  REFERENCES precincts(snapshot_id, key) ON DELETE CASCADE;
ALTER TABLE adjacency_edges
  ADD CONSTRAINT adjacency_edges_snapshot_target_fkey
  FOREIGN KEY (snapshot_id, target)
  REFERENCES precincts(snapshot_id, key) ON DELETE CASCADE;
