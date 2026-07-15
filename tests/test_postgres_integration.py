import os
from uuid import uuid4

import pytest

from gerry.cli import migrate
from gerry.domain import JobStatus, OptimizationRun
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
