import json
from pathlib import Path
from uuid import UUID, uuid4

import gerry.api as api_module
import geopandas as gpd
import pytest
from fastapi import HTTPException, Response
from shapely.geometry import box
from gerry.api import (
    _data_path,
    GraphCreate,
    build_graph,
    capabilities,
    create_scenario,
    create_optimization,
    export_optimization,
    get_graph,
    get_optimization_certificate,
    get_reconstruction_report,
    get_snapshot_precincts,
    health,
    list_optimizations,
    list_scenarios,
    profiles,
    readiness,
    small_example,
)
from gerry.domain import OptimizationRequest, VoteScenario
from gerry.services import optimization_service
from gerry.solver import ExactEnumerator

from test_solver import small_request


def test_health_and_profiles():
    assert health() == {"status": "ok"}
    assert "pl-sejm@2026-07-15" in profiles()
    system = capabilities()
    assert system["exhaustive_node_limit"] == 14
    assert len(system["law_profile_sha256"]) == 64
    assert system["certified_large_jobs"] == (
        system["exact_scip"] and system["viprcomp"] and system["viprchk"]
    )
    assert OptimizationRequest.model_validate(small_example()).nodes == ["a", "b", "c", "d"]


def test_packaged_example_matches_developer_copy():
    packaged = Path(api_module.__file__).with_name("resources") / "small_request.json"
    developer = Path("examples/small_request.json")
    assert packaged.read_text(encoding="utf-8").strip() == developer.read_text(
        encoding="utf-8"
    ).strip()


def test_readiness_reports_repository_failure(monkeypatch):
    response = Response()
    monkeypatch.setattr(
        optimization_service.repository,
        "healthcheck",
        lambda: (_ for _ in ()).throw(ConnectionError("offline")),
    )
    result = readiness(response)
    assert response.status_code == 503
    assert result["status"] == "not-ready"


def test_api_returns_created_run(monkeypatch, tmp_path):
    # Synchronous substitution makes the HTTP contract deterministic.
    monkeypatch.setattr(optimization_service, "submit", optimization_service.solve_now)
    run = create_optimization(small_request())
    assert run.status.value == "OPTIMAL"
    assert run.certificate_verified is True


def test_api_passes_bounded_pagination_to_repository(monkeypatch):
    captured = {}

    def fake_list(*, limit, offset):
        captured.update(limit=limit, offset=offset)
        return []

    monkeypatch.setattr(optimization_service.repository, "list", fake_list)
    assert list_optimizations(limit=25, offset=50) == []
    assert captured == {"limit": 25, "offset": 50}


def test_scenario_list_uses_paginated_metadata_not_full_votes(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module.settings, "data_dir", tmp_path)
    scenarios = [
        VoteScenario(
            id=UUID(int=index + 1),
            name=f"scenario-{index}",
            votes_by_unit={"a": {"X": index, "Y": 1}},
        )
        for index in range(3)
    ]
    for scenario in scenarios:
        create_scenario(scenario)

    page = list_scenarios(limit=1, offset=1)

    assert len(page) == 1
    assert page[0].id == scenarios[1].id
    assert page[0].unit_count == 1
    assert page[0].committee_count == 2
    assert not hasattr(page[0], "votes_by_unit")
    assert (tmp_path / "artifacts/scenarios/metadata" / f"{scenarios[1].id}.json").is_file()


def test_api_rejects_unknown_or_incompatible_optimization_profile():
    unknown = small_request()
    unknown.profile_id = "not-a-real-election-system"
    with pytest.raises(HTTPException) as error:
        create_optimization(unknown)
    assert error.value.status_code == 422

    proportional_candidate = small_request()
    proportional_candidate.profile_id = "generic-proportional"
    proportional_candidate.target_kind = "candidate"
    proportional_candidate.candidate_anchor = "a"
    with pytest.raises(HTTPException) as error:
        create_optimization(proportional_candidate)
    assert error.value.status_code == 422


def test_api_exports_selected_alternative_without_mutating_main_plan(monkeypatch, tmp_path):
    run = ExactEnumerator(tmp_path / "certificates").solve(small_request())
    assert run.incumbent is not None
    alternative = run.incumbent.model_copy(deep=True)
    alternative.assignment = {node: 1 - district for node, district in alternative.assignment.items()}
    run.alternatives = [alternative]
    main_assignment = dict(run.incumbent.assignment)
    captured = {}

    monkeypatch.setattr(optimization_service.repository, "get", lambda run_id: run)

    def fake_export(exported_run, output: Path, format: str):
        captured["assignment"] = dict(exported_run.incumbent.assignment)
        captured["format"] = format
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("{}", encoding="utf-8")
        return output

    monkeypatch.setattr(api_module, "export_run", fake_export)
    response = export_optimization(run.id, format="json", alternative=0)

    assert captured["assignment"] == run.alternatives[0].assignment
    assert captured["format"] == "json"
    assert response.filename.endswith("-alt-1.json")
    assert run.incumbent.assignment == main_assignment


def test_api_returns_only_certificate_bound_to_requested_run(monkeypatch, tmp_path):
    run = ExactEnumerator(tmp_path / "artifacts/certificates").solve(small_request())
    monkeypatch.setattr(api_module.settings, "data_dir", tmp_path)
    monkeypatch.setattr(optimization_service.repository, "get", lambda run_id: run)

    payload = get_optimization_certificate(run.id)

    assert payload["run_id"] == str(run.id)
    assert payload["request_sha256"]


def test_api_rejects_certificate_outside_data_directory(monkeypatch, tmp_path):
    run = ExactEnumerator(tmp_path / "artifacts/certificates").solve(small_request())
    run.certificate_path = "/etc/passwd"
    monkeypatch.setattr(api_module.settings, "data_dir", tmp_path)
    monkeypatch.setattr(optimization_service.repository, "get", lambda run_id: run)

    with pytest.raises(HTTPException) as error:
        get_optimization_certificate(run.id)

    assert error.value.status_code == 403


def test_api_paths_are_confined_to_data_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module.settings, "data_dir", tmp_path)
    source = tmp_path / "processed" / "precincts.gpkg"
    source.parent.mkdir(parents=True)
    source.touch()

    assert _data_path("processed/precincts.gpkg", label="Dane") == source
    assert _data_path(str(source), label="Dane") == source
    with pytest.raises(HTTPException) as error:
        _data_path("../../etc/passwd", label="Dane")
    assert error.value.status_code == 403


def test_api_builds_and_persists_graph_inside_snapshot(tmp_path, monkeypatch):
    snapshot_id = uuid4()
    monkeypatch.setattr(api_module.settings, "data_dir", tmp_path)
    monkeypatch.setattr(api_module.snapshot_store, "get", lambda candidate: object())
    root = tmp_path / "processed" / "snapshots" / str(snapshot_id)
    root.mkdir(parents=True)
    gpd.GeoDataFrame(
        {"key": ["a", "b"]},
        geometry=[box(0, 0, 10, 10), box(10, 0, 20, 10)],
        crs=2180,
    ).to_file(root / "precincts.gpkg", layer="precincts", driver="GPKG")

    result = build_graph(GraphCreate(snapshot_id=snapshot_id))

    assert result["snapshot_id"] == str(snapshot_id)
    assert result["nodes"] == 2
    assert result["node_ids"] == ["a", "b"]
    assert len(result["edges"]) == 1
    assert result["build_parameters"]["boundary_tolerance_m"] == 0.01
    assert result["path"].endswith(f"{snapshot_id}/graph.json")
    assert (root / "graph.json").is_file()

    loaded = get_graph(snapshot_id)
    assert loaded["node_ids"] == ["a", "b"]


def test_api_returns_snapshot_precinct_geometry_as_geojson(tmp_path, monkeypatch):
    snapshot_id = uuid4()
    monkeypatch.setattr(api_module.settings, "data_dir", tmp_path)
    monkeypatch.setattr(api_module.snapshot_store, "get", lambda candidate: object())
    root = tmp_path / "processed/snapshots" / str(snapshot_id)
    root.mkdir(parents=True)
    gpd.GeoDataFrame(
        {"key": ["a", "b"], "private_column": ["secret-a", "secret-b"]},
        geometry=[box(0, 0, 10, 10), box(10, 0, 20, 10)],
        crs=2180,
    ).to_file(root / "precincts.gpkg", layer="precincts", driver="GPKG")

    payload = get_snapshot_precincts(snapshot_id)

    assert payload["type"] == "FeatureCollection"
    assert [feature["properties"] for feature in payload["features"]] == [
        {"node": "a"},
        {"node": "b"},
    ]
    assert all(feature["geometry"]["type"] == "Polygon" for feature in payload["features"])


def test_api_rejects_legacy_graph_without_node_ids(tmp_path, monkeypatch):
    snapshot_id = uuid4()
    monkeypatch.setattr(api_module.settings, "data_dir", tmp_path)
    monkeypatch.setattr(api_module.snapshot_store, "get", lambda candidate: object())
    root = tmp_path / "processed/snapshots" / str(snapshot_id)
    root.mkdir(parents=True)
    (root / "graph.json").write_text(
        json.dumps({"snapshot_id": str(snapshot_id), "nodes": 1, "edges": []}),
        encoding="utf-8",
    )

    with pytest.raises(HTTPException) as error:
        get_graph(snapshot_id)

    assert error.value.status_code == 409


def test_reconstruction_report_is_filtered_and_paginated(tmp_path, monkeypatch):
    snapshot_id = uuid4()
    monkeypatch.setattr(api_module.settings, "data_dir", tmp_path)
    monkeypatch.setattr(api_module.snapshot_store, "get", lambda candidate: object())
    root = tmp_path / "artifacts/reconstruction" / str(snapshot_id)
    root.mkdir(parents=True)
    (root / "national.json").write_text(
        json.dumps([
            {"teryt": "1"},
            {"teryt": "2", "error": "failure-2"},
            {"teryt": "3", "error": "failure-3"},
        ]),
        encoding="utf-8",
    )
    (root / "run_manifest.json").write_text(
        json.dumps({"successful": 1, "failed": 2, "complete_country": False}),
        encoding="utf-8",
    )

    payload = get_reconstruction_report(
        snapshot_id, limit=1, offset=1, failed_only=True
    )

    assert payload["total"] == 2
    assert payload["reports"] == [{"teryt": "3", "error": "failure-3"}]
    assert payload["manifest"]["failed"] == 2
