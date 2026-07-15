import json

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

import gerry.pipeline as pipeline_module
from gerry.pipeline import NationalReconstructionPipeline


def test_reconstruction_outputs_are_isolated_by_snapshot(tmp_path):
    first = NationalReconstructionPipeline(tmp_path, snapshot_id="snapshot-a")
    second = NationalReconstructionPipeline(tmp_path, snapshot_id="snapshot-b")

    assert first.output_dir == tmp_path / "processed/snapshots/snapshot-a/precincts"
    assert second.output_dir == tmp_path / "processed/snapshots/snapshot-b/precincts"
    assert first.output_dir != second.output_dir
    assert first.report_dir == tmp_path / "artifacts/reconstruction/snapshot-a"


def test_national_pipeline_reports_every_registry_teryt_missing_a_boundary(
    tmp_path, monkeypatch
):
    registry = pd.DataFrame({
        "teryt": ["020101", "020102"],
        "special": [False, False],
    })
    boundaries = gpd.GeoDataFrame(
        {"teryt": ["020101"]}, geometry=[box(0, 0, 1, 1)], crs=4326
    )
    monkeypatch.setattr(pipeline_module, "load_registry", lambda path: registry)
    pipeline = NationalReconstructionPipeline(tmp_path, snapshot_id="snapshot")

    def reconstruct(teryt, registry, boundary, *, force=False):
        del registry, boundary, force
        frame = gpd.GeoDataFrame(
            {"key": [f"{teryt}_1"]}, geometry=[box(0, 0, 1, 1)], crs=4326
        )
        return frame, {"teryt": teryt}

    monkeypatch.setattr(pipeline, "reconstruct_gmina", reconstruct)

    reports = pipeline.run(tmp_path / "registry.xlsx", boundaries)

    assert reports[0] == {"teryt": "020101"}
    assert reports[1]["teryt"] == "020102"
    assert reports[1]["error"].startswith("MissingBoundary")
    assert not (pipeline.report_dir / "national.json.part").exists()


def test_retry_failed_rebuilds_country_from_all_cached_municipalities(
    tmp_path, monkeypatch
):
    registry = pd.DataFrame({
        "teryt": ["020101", "020102"],
        "special": [False, False],
    })
    boundaries = gpd.GeoDataFrame(
        {"teryt": ["020101", "020102"]},
        geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1)],
        crs=4326,
    )
    monkeypatch.setattr(pipeline_module, "load_registry", lambda path: registry)
    pipeline = NationalReconstructionPipeline(tmp_path, snapshot_id="snapshot")

    def frame_for(teryt):
        return gpd.GeoDataFrame(
            {"key": [f"{teryt}_1"], "teryt": [teryt]},
            geometry=[box(0, 0, 1, 1)],
            crs=4326,
        )

    def first_pass(teryt, registry, boundary, *, force=False):
        del registry, boundary, force
        if teryt == "020102":
            raise RuntimeError("temporary source failure")
        frame = frame_for(teryt)
        frame.to_parquet(pipeline.output_dir / f"{teryt}.parquet")
        return frame, {"teryt": teryt}

    monkeypatch.setattr(pipeline, "reconstruct_gmina", first_pass)
    first = pipeline.run(tmp_path / "registry.xlsx", boundaries)
    assert ["error" in report for report in first] == [False, True]

    def retry(teryt, registry, boundary, *, force=False):
        del registry, boundary, force
        frame = frame_for(teryt)
        frame.to_parquet(pipeline.output_dir / f"{teryt}.parquet")
        return frame, {"teryt": teryt}

    monkeypatch.setattr(pipeline, "reconstruct_gmina", retry)
    retried = pipeline.run(
        tmp_path / "registry.xlsx", boundaries, retry_failed=True
    )
    country = gpd.read_parquet(pipeline.processed_root / "precincts.parquet")

    assert retried == [{"teryt": "020102"}]
    assert sorted(country.key) == ["020101_1", "020102_1"]
    manifest = json.loads(
        (pipeline.report_dir / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["cached_municipalities"] == 2
    assert manifest["complete_country"] is True
    cumulative = json.loads(
        (pipeline.report_dir / "national.json").read_text(encoding="utf-8")
    )
    assert cumulative == [{"teryt": "020101"}, {"teryt": "020102"}]
