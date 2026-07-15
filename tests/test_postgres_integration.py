import os
from datetime import date
from uuid import uuid4

import geopandas as gpd
import pytest
from shapely.geometry import box

from gerry.cli import migrate
from gerry.domain import AdjacencyEdge, DataSnapshot, JobStatus, OptimizationRun
from gerry.postgis_sync import sync_snapshot_to_postgis
from gerry.repository import PostgresRunRepository
from gerry.settings import settings

from test_solver import small_request


POSTGRES_URL = os.getenv("GERRY_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="wymaga testowego PostGIS")


def test_packaged_migration_and_transactional_queue_on_postgis(monkeypatch):
    import psycopg

    monkeypatch.setattr(settings, "database_url", POSTGRES_URL)
    migrate()
    dsn = POSTGRES_URL.replace("postgresql+psycopg://", "postgresql://")

    with psycopg.connect(dsn) as connection:
        connection.execute("TRUNCATE optimization_runs")
        geometry = connection.execute(
            """
            SELECT type, srid
            FROM geometry_columns
            WHERE f_table_name = 'precincts' AND f_geometry_column = 'geometry'
            """
        ).fetchone()
    assert geometry == ("MULTIPOLYGON", 2180)

    repository = PostgresRunRepository(POSTGRES_URL)
    queued = OptimizationRun(request=small_request())
    repository.save(queued)

    claimed = repository.claim_next()
    assert claimed is not None
    assert claimed.id == queued.id
    assert claimed.status == JobStatus.running
    assert repository.claim_next() is None
    assert repository.get(queued.id).status == JobStatus.running
    assert [run.id for run in repository.list(limit=1, offset=0)] == [queued.id]


def test_precinct_keys_and_edges_are_scoped_to_snapshot(monkeypatch):
    import psycopg

    monkeypatch.setattr(settings, "database_url", POSTGRES_URL)
    migrate()
    dsn = POSTGRES_URL.replace("postgresql+psycopg://", "postgresql://")
    first, second = uuid4(), uuid4()
    with psycopg.connect(dsn) as connection:
        connection.execute("TRUNCATE data_snapshots CASCADE")
        connection.executemany(
            """
            INSERT INTO data_snapshots(id, election_id, effective_date, status)
            VALUES (%s, %s, DATE '2026-01-01', 'READY')
            """,
            [(first, "first"), (second, "second")],
        )
        for snapshot in (first, second):
            connection.executemany(
                """
                INSERT INTO precincts(
                    key, snapshot_id, teryt, number, quality
                ) VALUES (%s, %s, '020101', %s, 'official')
                """,
                [("same-key", snapshot, 1), ("z", snapshot, 2)],
            )
        connection.execute(
            """
            INSERT INTO precincts(key, snapshot_id, teryt, number, quality)
            VALUES ('only-second', %s, '020101', 3, 'official')
            """,
            (second,),
        )
        connection.execute(
            """
            INSERT INTO adjacency_edges(snapshot_id, source, target, shared_border_m)
            VALUES (%s, 'same-key', 'z', 10)
            """,
            (first,),
        )
        count = connection.execute(
            "SELECT count(*) FROM precincts WHERE key = 'same-key'"
        ).fetchone()[0]

    assert count == 2
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with psycopg.connect(dsn) as connection:
            connection.execute(
                """
                INSERT INTO adjacency_edges(
                    snapshot_id, source, target, shared_border_m
                ) VALUES (%s, 'only-second', 'same-key', 10)
                """,
                (first,),
            )


def test_snapshot_geometry_and_graph_sync_is_transactional_and_idempotent(monkeypatch):
    import psycopg

    monkeypatch.setattr(settings, "database_url", POSTGRES_URL)
    migrate()
    snapshot = DataSnapshot(
        election_id="sync-test",
        effective_date=date(2026, 1, 1),
        status="READY",
    )
    frame = gpd.GeoDataFrame(
        {
            "key": ["a", "b"],
            "teryt": ["020101", "020101"],
            "precinct": [1, 2],
            "eligible": [100, 120],
            "geometry_quality": ["generated", "generated"],
        },
        geometry=[box(19, 52, 19.01, 52.01), box(19.01, 52, 19.02, 52.01)],
        crs=4326,
    )
    edges = [AdjacencyEdge(source="a", target="b", shared_border_m=100)]

    first = sync_snapshot_to_postgis(snapshot, frame, edges, POSTGRES_URL)
    second = sync_snapshot_to_postgis(snapshot, frame, edges, POSTGRES_URL)

    assert first == second == {"snapshots": 1, "artifacts": 0, "precincts": 2, "edges": 1}
    dsn = POSTGRES_URL.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            """
            SELECT count(*), min(ST_SRID(geometry)), count(*) FILTER (WHERE ST_IsValid(geometry))
            FROM precincts WHERE snapshot_id = %s
            """,
            (snapshot.id,),
        ).fetchone()
        edge_count = connection.execute(
            "SELECT count(*) FROM adjacency_edges WHERE snapshot_id = %s",
            (snapshot.id,),
        ).fetchone()[0]
    assert row == (2, 2180, 2)
    assert edge_count == 1
