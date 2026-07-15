import json
from datetime import date

import geopandas as gpd
import pytest
import typer
from shapely.geometry import box

from gerry import cli
from gerry.domain import JobStatus
from gerry import scip_solver
from gerry import postgis_sync as postgis_sync_module
from gerry.snapshots import SnapshotStore


def test_mapa_obwodow_import_places_prg_in_pipeline_cache(tmp_path, monkeypatch):
    source = tmp_path / "mapa_obwodow"
    (source / "data/metadata").mkdir(parents=True)
    (source / "data/raw/prg").mkdir(parents=True)
    (source / "data/raw/sejm2023").mkdir(parents=True)
    (source / "data/metadata/obwody_glosowania_utf8.xlsx").write_bytes(b"registry")
    (source / "data/raw/prg/020101.parquet").write_bytes(b"cache")
    (source / "data/raw/sejm2023/results.zip").write_bytes(b"results")
    monkeypatch.setattr(cli.settings, "data_dir", tmp_path / "target")

    cli.import_mapa_obwodow(source, "sejm2023")

    assert (tmp_path / "target/raw/prg/020101.parquet").read_bytes() == b"cache"
    assert (tmp_path / "target/raw/elections/sejm2023/results.zip").read_bytes() == b"results"
    assert (
        tmp_path / "target/raw/imports/mapa_obwodow/sejm2023/obwody_glosowania_utf8.xlsx"
    ).read_bytes() == b"registry"


def test_solver_smoke_requires_verified_optimum(tmp_path, monkeypatch):
    class SuccessfulSolver:
        def __init__(self, artifact_dir):
            self.infeasible = artifact_dir == tmp_path / "artifacts/smoke-infeasible"
            assert artifact_dir in {
                tmp_path / "artifacts/smoke",
                tmp_path / "artifacts/smoke-infeasible",
            }

        def solve(self, request):
            assert request.nodes == ["a", "b", "c", "d"]
            manifest = tmp_path / (
                "infeasible-certificate.json" if self.infeasible else "optimal-certificate.json"
            )
            manifest.write_text(
                json.dumps({
                    "schema_version": 2,
                    "request_sha256": "a" * 64,
                    "proofs": [{"model_sha256": "b" * 64, "proof_sha256": "c" * 64}],
                }),
                encoding="utf-8",
            )
            return type("Run", (), {
                "status": JobStatus.infeasible if self.infeasible else JobStatus.optimal,
                "certificate_verified": True,
                "certificate_path": str(manifest),
                "message": "OK",
            })()

    monkeypatch.setattr(cli.settings, "data_dir", tmp_path)
    monkeypatch.setattr(scip_solver, "ScipExactSolver", SuccessfulSolver)
    cli.solver_smoke()


def test_graph_cli_persists_versioned_api_compatible_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.settings, "data_dir", tmp_path)
    snapshot = SnapshotStore(tmp_path / "raw/snapshots").create(
        "test-election", date(2026, 1, 1)
    )
    snapshot_root = tmp_path / "processed/snapshots" / str(snapshot.id)
    snapshot_root.mkdir(parents=True)
    source = snapshot_root / "precincts.gpkg"
    output = snapshot_root / "graph.json"
    gpd.GeoDataFrame(
        {"key": ["a", "b"]},
        geometry=[box(0, 0, 10, 10), box(10, 0, 20, 10)],
        crs=2180,
    ).to_file(source, layer="precincts", driver="GPKG")

    cli.graph_build(source, output, str(snapshot.id))

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["snapshot_id"] == str(snapshot.id)
    assert payload["nodes"] == 2
    assert payload["node_ids"] == ["a", "b"]
    assert len(payload["edges"]) == 1
    assert payload["errors"] == []
    assert not output.with_suffix(".json.part").exists()


def test_graph_cli_rejects_source_from_another_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.settings, "data_dir", tmp_path)
    snapshot = SnapshotStore(tmp_path / "raw/snapshots").create(
        "test-election", date(2026, 1, 1)
    )
    foreign = tmp_path / "foreign.gpkg"
    foreign.touch()
    output = tmp_path / "processed/snapshots" / str(snapshot.id) / "graph.json"

    with pytest.raises(typer.BadParameter, match="musi należeć"):
        cli.graph_build(foreign, output, str(snapshot.id))


def test_postgis_sync_cli_uses_snapshot_scoped_artifacts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.settings, "data_dir", tmp_path)
    monkeypatch.setattr(cli.settings, "database_url", "postgresql://example")
    snapshot = SnapshotStore(tmp_path / "raw/snapshots").create(
        "test-election", date(2026, 1, 1)
    )
    root = tmp_path / "processed/snapshots" / str(snapshot.id)
    root.mkdir(parents=True)
    gpd.GeoDataFrame(
        {"key": ["a"], "teryt": ["020101"], "precinct": [1]},
        geometry=[box(0, 0, 1, 1)],
        crs=2180,
    ).to_file(root / "precincts.gpkg", layer="precincts", driver="GPKG")
    (root / "graph.json").write_text(
        json.dumps({
            "snapshot_id": str(snapshot.id),
            "node_ids": ["a"],
            "edges": [],
        }),
        encoding="utf-8",
    )
    captured = {}

    def fake_sync(received_snapshot, frame, edges, database_url):
        captured.update(
            snapshot=received_snapshot,
            frame=frame,
            edges=edges,
            database_url=database_url,
        )
        return {"snapshots": 1, "artifacts": 0, "precincts": 1, "edges": 0}

    monkeypatch.setattr(postgis_sync_module, "sync_snapshot_to_postgis", fake_sync)

    cli.postgis_sync(str(snapshot.id))

    assert captured["snapshot"].id == snapshot.id
    assert captured["frame"].key.tolist() == ["a"]
    assert captured["edges"] == []
    assert captured["database_url"] == "postgresql://example"
    assert "obwody=1" in capsys.readouterr().out
