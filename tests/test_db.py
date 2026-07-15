from importlib.resources import files
from pathlib import Path

from sqlalchemy import ForeignKeyConstraint

from gerry.db import Base, EdgeRow, PrecinctRow, make_engine


def test_schema_can_be_created_in_sqlite(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    assert set(Base.metadata.tables) == {
        "data_snapshots", "source_artifacts", "precincts", "adjacency_edges", "optimization_runs"
    }


def test_precinct_geometry_attribute_matches_postgis_migration():
    assert PrecinctRow.__table__.c.geometry.name == "geometry"
    assert "geometry_ewkb" not in PrecinctRow.__table__.c
    migration = files("gerry").joinpath("resources/001_initial.sql").read_text(encoding="utf-8")
    assert "geometry geometry(MultiPolygon,2180)" in migration
    assert set(PrecinctRow.__table__.primary_key.columns.keys()) == {
        "key", "snapshot_id"
    }
    composite_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in EdgeRow.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint) and len(constraint.elements) == 2
    }
    assert composite_targets == {
        ("precincts.snapshot_id", "precincts.key")
    }


def test_packaged_migration_matches_developer_copy():
    for name in ["001_initial.sql", "002_snapshot_scoped_precinct_keys.sql"]:
        packaged = files("gerry").joinpath(f"resources/{name}").read_text(
            encoding="utf-8"
        )
        developer = Path("migrations", name).read_text(encoding="utf-8")
        assert packaged.strip() == developer.strip()
