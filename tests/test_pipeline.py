import json
import threading
import time

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon, box

import gerry.pipeline as pipeline_module
from gerry.pipeline import NationalReconstructionPipeline


def test_reconstruction_outputs_are_isolated_by_snapshot(tmp_path):
    first = NationalReconstructionPipeline(tmp_path, snapshot_id="snapshot-a")
    second = NationalReconstructionPipeline(tmp_path, snapshot_id="snapshot-b")

    assert first.output_dir == tmp_path / "processed/snapshots/snapshot-a/precincts"
    assert second.output_dir == tmp_path / "processed/snapshots/snapshot-b/precincts"
    assert first.output_dir != second.output_dir
    assert first.report_dir == tmp_path / "artifacts/reconstruction/snapshot-a"


def test_national_pipeline_rejects_a_second_writer_for_the_same_snapshot(
    tmp_path,
):
    if pipeline_module.fcntl is None:
        return
    pipeline = NationalReconstructionPipeline(tmp_path, snapshot_id="snapshot")
    lock_path = pipeline.report_dir / ".run.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        pipeline_module.fcntl.flock(
            handle, pipeline_module.fcntl.LOCK_EX | pipeline_module.fcntl.LOCK_NB
        )
        try:
            with pytest.raises(RuntimeError, match="Inny przebieg rekonstrukcji"):
                pipeline.run(tmp_path / "registry.xlsx")
        finally:
            pipeline_module.fcntl.flock(handle, pipeline_module.fcntl.LOCK_UN)


def test_national_area_list_merges_warsaw_and_excludes_foreign_precincts(tmp_path):
    pipeline = NationalReconstructionPipeline(tmp_path, snapshot_id="snapshot")
    registry = pd.DataFrame({
        "teryt": ["020101", "", "146502", "146503", "146519"],
        "precinct": [1, 2, 711, 749, 999],
    })

    assert pipeline._area_teryts(registry) == ["020101", "146501"]
    warsaw = pipeline._registry_for_area(registry, "146501")
    assert warsaw.teryt.tolist() == ["146502", "146503", "146519"]


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


def test_national_pipeline_uses_parallel_workers(tmp_path, monkeypatch):
    teryts = [f"02010{number}" for number in range(1, 5)]
    registry = pd.DataFrame({"teryt": teryts, "special": [False] * 4})
    boundaries = gpd.GeoDataFrame(
        {"teryt": teryts},
        geometry=[box(number, 0, number + 1, 1) for number in range(4)],
        crs=4326,
    )
    monkeypatch.setattr(pipeline_module, "load_registry", lambda path: registry)
    pipeline = NationalReconstructionPipeline(tmp_path, snapshot_id="snapshot")
    lock = threading.Lock()
    active = 0
    maximum = 0
    progress = []

    def reconstruct(teryt, registry, boundary, *, force=False):
        nonlocal active, maximum
        del registry, boundary, force
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return None, {"teryt": teryt}

    monkeypatch.setattr(pipeline, "reconstruct_gmina", reconstruct)

    reports = pipeline.run(
        tmp_path / "registry.xlsx",
        boundaries,
        workers=4,
        progress_callback=lambda completed, total, report: progress.append(
            (completed, total, report)
        ),
    )

    assert [report["teryt"] for report in reports] == teryts
    assert maximum > 1
    assert progress[0] == (0, 4, None)
    assert progress[-1][0:2] == (4, 4)
    manifest = json.loads(
        (pipeline.report_dir / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["workers"] == 4


def test_national_pipeline_repairs_invalid_boundary_without_stopping_country(
    tmp_path, monkeypatch
):
    registry = pd.DataFrame({"teryt": ["020101"], "special": [False]})
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
    boundaries = gpd.GeoDataFrame(
        {"teryt": ["020101"]}, geometry=[bowtie], crs=4326
    )
    monkeypatch.setattr(pipeline_module, "load_registry", lambda path: registry)
    pipeline = NationalReconstructionPipeline(tmp_path, snapshot_id="snapshot")

    def reconstruct(teryt, registry, boundary, *, force=False):
        del registry, force
        assert boundary.is_valid
        return None, {"teryt": teryt}

    monkeypatch.setattr(pipeline, "reconstruct_gmina", reconstruct)

    assert pipeline.run(
        tmp_path / "registry.xlsx", boundaries, workers=2
    ) == [{"teryt": "020101"}]


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
    assert manifest["processed_this_run"] == 1
    assert manifest["successful_this_run"] == 1
    assert manifest["successful"] == 2
    assert manifest["failed"] == 0
    assert manifest["complete_country"] is True
    cumulative = json.loads(
        (pipeline.report_dir / "national.json").read_text(encoding="utf-8")
    )
    assert cumulative == [{"teryt": "020101"}, {"teryt": "020102"}]
