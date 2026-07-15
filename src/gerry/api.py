from __future__ import annotations

import json
from typing import Annotated
from datetime import date
from pathlib import Path
from uuid import UUID

import geopandas as gpd
from fastapi import FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .domain import DataSnapshot, DistrictPlan, OptimizationRequest, OptimizationRun, VoteScenario
from .graph import build_adjacency, validate_graph
from .law import LAW_PROFILE_SHA256, LegalValidator, PROFILE_CITATIONS
from .pipeline import NationalReconstructionPipeline
from .snapshots import SnapshotStore
from .exports import export_run
from .settings import settings
from .services import optimization_service
from .scip_solver import exact_scip_available, verify_vipr_manifest


app = FastAPI(
    title="Gerrymandering PL",
    version="0.1.0",
    description="Rekonstrukcja obwodów, walidacja i certyfikowana optymalizacja podziałów.",
)
snapshot_store = SnapshotStore(settings.raw_dir / "snapshots")


class SnapshotCreate(BaseModel):
    election_id: str = Field(min_length=1)
    effective_date: date


class ReconstructionCreate(BaseModel):
    snapshot_id: UUID
    registry_path: str
    boundaries_path: str | None = None
    limit: int | None = Field(default=None, gt=0)
    teryts: list[str] | None = None
    retry_failed: bool = False
    force: bool = False
    workers: int = Field(default=0, ge=0)


class GraphCreate(BaseModel):
    snapshot_id: UUID
    source_path: str | None = None
    key_column: str = Field(default="key", min_length=1)
    min_shared_border_m: float = Field(default=1.0, ge=0)
    boundary_tolerance_m: float = Field(default=0.01, ge=0)


class ValidationCreate(BaseModel):
    request: OptimizationRequest
    plan: DistrictPlan


class ScenarioSummary(BaseModel):
    id: UUID
    name: str
    snapshot_id: UUID | None = None
    unit_count: int = Field(ge=0)
    committee_count: int = Field(ge=0)


def _scenario_summary(scenario: VoteScenario) -> ScenarioSummary:
    committees = {
        committee
        for votes in scenario.votes_by_unit.values()
        for committee in votes
    }
    return ScenarioSummary(
        id=scenario.id,
        name=scenario.name,
        snapshot_id=scenario.snapshot_id,
        unit_count=len(scenario.votes_by_unit),
        committee_count=len(committees),
    )


def _atomic_model_write(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(model.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)


def _data_path(value: str, *, label: str) -> Path:
    """Resolve an API-supplied path without allowing reads outside GERRY_DATA_DIR."""
    root = settings.data_dir.resolve()
    supplied = Path(value)
    resolved = (supplied if supplied.is_absolute() else root / supplied).resolve()
    if not resolved.is_relative_to(root):
        raise HTTPException(status_code=403, detail=f"{label} musi znajdować się w katalogu danych")
    return resolved


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def readiness(response: Response) -> dict:
    try:
        optimization_service.repository.healthcheck()
        settings.ensure_dirs()
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not-ready", "detail": f"{type(exc).__name__}: {exc}"}
    return {"status": "ready"}


@app.get("/api/system/capabilities")
def capabilities() -> dict:
    exact, detail = exact_scip_available()
    import shutil

    return {
        "exact_scip": exact,
        "scip_detail": detail,
        "viprcomp": shutil.which("viprcomp") is not None,
        "viprchk": shutil.which("viprchk") is not None,
        "certified_large_jobs": bool(
            exact and shutil.which("viprcomp") and shutil.which("viprchk")
        ),
        "exhaustive_node_limit": 14,
        "law_snapshot": settings.law_snapshot,
        "law_profile_sha256": LAW_PROFILE_SHA256,
    }


@app.get("/api/profiles")
def profiles() -> dict[str, str]:
    return PROFILE_CITATIONS


@app.get("/api/examples/small")
def small_example() -> dict:
    path = Path(__file__).with_name("resources") / "small_request.json"
    if not path.exists():  # editable checkout; wheel contains the packaged copy
        path = Path(__file__).resolve().parents[2] / "examples" / "small_request.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Brak przykładu")
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/snapshots", response_model=DataSnapshot, status_code=201)
def create_snapshot(body: SnapshotCreate) -> DataSnapshot:
    return snapshot_store.create(body.election_id, body.effective_date)


@app.get("/api/snapshots/{snapshot_id}", response_model=DataSnapshot)
def get_snapshot(snapshot_id: UUID) -> DataSnapshot:
    snapshot = snapshot_store.get(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Nie znaleziono migawki")
    return snapshot


@app.get("/api/snapshots", response_model=list[DataSnapshot])
def list_snapshots() -> list[DataSnapshot]:
    return snapshot_store.list()


@app.get("/api/snapshots/{snapshot_id}/precincts")
def get_snapshot_precincts(snapshot_id: UUID) -> dict:
    if snapshot_store.get(snapshot_id) is None:
        raise HTTPException(status_code=404, detail="Nie znaleziono migawki")
    path = settings.processed_dir / "snapshots" / str(snapshot_id) / "precincts.gpkg"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Brak geometrii obwodów migawki")
    frame = gpd.read_file(path)
    if frame.crs is None or "key" not in frame or frame["key"].astype(str).duplicated().any():
        raise HTTPException(status_code=409, detail="Niepoprawna warstwa obwodów migawki")
    if frame.geometry.isna().any() or frame.geometry.is_empty.any() or (~frame.geometry.is_valid).any():
        raise HTTPException(status_code=409, detail="Warstwa zawiera niepoprawne geometrie")
    geometry = frame.to_crs(4326)
    payload = gpd.GeoDataFrame(
        {"node": geometry["key"].astype(str)},
        geometry=geometry.geometry,
        crs=4326,
    )
    return json.loads(payload.to_json(drop_id=True))


@app.post("/api/scenarios", response_model=VoteScenario, status_code=201)
def create_scenario(scenario: VoteScenario) -> VoteScenario:
    root = settings.artifacts_dir / "scenarios"
    _atomic_model_write(root / f"{scenario.id}.json", scenario)
    _atomic_model_write(root / "metadata" / f"{scenario.id}.json", _scenario_summary(scenario))
    return scenario


@app.get("/api/scenarios/{scenario_id}", response_model=VoteScenario)
def get_scenario(scenario_id: UUID) -> VoteScenario:
    path = settings.artifacts_dir / "scenarios" / f"{scenario_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Nie znaleziono scenariusza")
    return VoteScenario.model_validate_json(path.read_text(encoding="utf-8"))


@app.get("/api/scenarios", response_model=list[ScenarioSummary])
def list_scenarios(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ScenarioSummary]:
    root = settings.artifacts_dir / "scenarios"
    if not root.exists():
        return []
    paths = sorted(root.glob("*.json"), reverse=True)[offset : offset + limit]
    summaries = []
    for path in paths:
        metadata = root / "metadata" / path.name
        if metadata.is_file():
            summaries.append(ScenarioSummary.model_validate_json(metadata.read_text(encoding="utf-8")))
            continue
        # Backward compatibility for scenarios created before metadata sidecars.
        scenario = VoteScenario.model_validate_json(path.read_text(encoding="utf-8"))
        summary = _scenario_summary(scenario)
        _atomic_model_write(metadata, summary)
        summaries.append(summary)
    return summaries


@app.post("/api/reconstruction")
def run_reconstruction(body: ReconstructionCreate) -> dict:
    if snapshot_store.get(body.snapshot_id) is None:
        raise HTTPException(status_code=404, detail="Nie znaleziono migawki")
    registry = _data_path(body.registry_path, label="Rejestr")
    if not registry.exists():
        raise HTTPException(status_code=404, detail="Brak rejestru")
    boundaries = None
    if body.boundaries_path:
        boundary_path = _data_path(body.boundaries_path, label="Warstwa granic")
        if not boundary_path.exists():
            raise HTTPException(status_code=404, detail="Brak warstwy granic")
        boundaries = gpd.read_file(boundary_path)
    reports = NationalReconstructionPipeline(
        settings.data_dir, snapshot_id=str(body.snapshot_id)
    ).run(
        registry,
        boundaries,
        limit=body.limit,
        teryts=body.teryts,
        retry_failed=body.retry_failed,
        force=body.force,
        workers=body.workers,
    )
    return {"processed": len(reports), "failed": sum("error" in report for report in reports), "reports": reports}


@app.get("/api/reconstruction/{snapshot_id}/report")
def get_reconstruction_report(
    snapshot_id: UUID,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    failed_only: bool = False,
) -> dict:
    if snapshot_store.get(snapshot_id) is None:
        raise HTTPException(status_code=404, detail="Nie znaleziono migawki")
    root = settings.artifacts_dir / "reconstruction" / str(snapshot_id)
    report_path = root / "national.json"
    if not report_path.is_file():
        raise HTTPException(status_code=404, detail="Brak raportu rekonstrukcji")
    reports = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(reports, list) or any(not isinstance(item, dict) for item in reports):
        raise HTTPException(status_code=409, detail="Niepoprawny format raportu rekonstrukcji")
    selected = [item for item in reports if not failed_only or "error" in item]
    manifest_path = root / "run_manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else None
    )
    return {
        "snapshot_id": str(snapshot_id),
        "total": len(selected),
        "limit": limit,
        "offset": offset,
        "failed_only": failed_only,
        "manifest": manifest,
        "reports": selected[offset : offset + limit],
    }


@app.post("/api/graphs/build")
def build_graph(body: GraphCreate) -> dict:
    if snapshot_store.get(body.snapshot_id) is None:
        raise HTTPException(status_code=404, detail="Nie znaleziono migawki")
    snapshot_root = (
        settings.processed_dir / "snapshots" / str(body.snapshot_id)
    ).resolve()
    default_source = snapshot_root / "precincts.gpkg"
    source = (
        _data_path(body.source_path, label="Warstwa geometrii")
        if body.source_path
        else default_source
    )
    if not source.is_relative_to(snapshot_root):
        raise HTTPException(
            status_code=409, detail="Warstwa geometrii nie należy do wskazanej migawki"
        )
    if not source.exists():
        raise HTTPException(status_code=404, detail="Brak warstwy geometrii")
    frame = gpd.read_file(source)
    try:
        edges = build_adjacency(
            frame, key_column=body.key_column,
            min_shared_border_m=body.min_shared_border_m,
            boundary_tolerance_m=body.boundary_tolerance_m,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    node_ids = frame[body.key_column].astype(str).tolist()
    payload = {
        "snapshot_id": str(body.snapshot_id),
        "nodes": len(frame),
        "node_ids": node_ids,
        "edges": [edge.model_dump() for edge in edges],
        "errors": validate_graph(node_ids, edges),
        "build_parameters": {
            "key_column": body.key_column,
            "metric_crs": 2180,
            "min_shared_border_m": body.min_shared_border_m,
            "boundary_tolerance_m": body.boundary_tolerance_m,
        },
    }
    output = snapshot_root / "graph.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".json.part")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    payload["path"] = str(output.relative_to(settings.data_dir.resolve()))
    return payload


@app.get("/api/graphs/{snapshot_id}")
def get_graph(snapshot_id: UUID) -> dict:
    if snapshot_store.get(snapshot_id) is None:
        raise HTTPException(status_code=404, detail="Nie znaleziono migawki")
    path = settings.processed_dir / "snapshots" / str(snapshot_id) / "graph.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Nie znaleziono grafu migawki")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if str(payload.get("snapshot_id")) != str(snapshot_id):
        raise HTTPException(status_code=409, detail="Graf nie należy do migawki")
    if "node_ids" not in payload:
        raise HTTPException(status_code=409, detail="Graf ma przestarzały format; zbuduj go ponownie")
    return payload


@app.post("/api/plans/validate")
def validate_plan(body: ValidationCreate):
    return LegalValidator().validate(body.request, body.plan)


@app.post("/api/optimizations", response_model=OptimizationRun, status_code=202)
def create_optimization(request: OptimizationRequest) -> OptimizationRun:
    try:
        return optimization_service.submit(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/optimizations", response_model=list[OptimizationRun])
def list_optimizations(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[OptimizationRun]:
    return optimization_service.repository.list(limit=limit, offset=offset)


@app.get("/api/optimizations/{run_id}", response_model=OptimizationRun)
def get_optimization(run_id: UUID) -> OptimizationRun:
    run = optimization_service.repository.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Nie znaleziono zadania")
    return run


@app.post("/api/optimizations/{run_id}/cancel", response_model=OptimizationRun)
def cancel_optimization(run_id: UUID) -> OptimizationRun:
    run = optimization_service.cancel(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Nie znaleziono zadania")
    return run


@app.get("/api/optimizations/{run_id}/certificate")
def get_optimization_certificate(run_id: UUID) -> dict:
    run = optimization_service.repository.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Nie znaleziono zadania")
    if not run.certificate_path:
        raise HTTPException(status_code=409, detail="Zadanie nie ma certyfikatu")
    path = _data_path(run.certificate_path, label="Certyfikat")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Brak pliku certyfikatu")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Nie można odczytać certyfikatu") from exc
    if str(payload.get("run_id")) != str(run.id):
        raise HTTPException(status_code=409, detail="Certyfikat nie należy do zadania")
    if payload.get("algorithm") == "scip-exact-lexicographic-vipr-v2":
        integrity, detail = verify_vipr_manifest(path, run.request)
        payload["integrity_verified"] = integrity
        payload["integrity_detail"] = detail
    return payload


@app.get("/api/optimizations/{run_id}/export")
def export_optimization(run_id: UUID, format: str = "geojson", alternative: int | None = None):
    run = optimization_service.repository.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Nie znaleziono zadania")
    if run.incumbent is None:
        raise HTTPException(status_code=409, detail="Zadanie nie ma jeszcze planu")
    exported_run = run
    alternative_suffix = ""
    if alternative is not None:
        if alternative < 0 or alternative >= len(run.alternatives):
            raise HTTPException(status_code=404, detail="Nie znaleziono alternatywnego planu")
        exported_run = run.model_copy(deep=True)
        exported_run.incumbent = run.alternatives[alternative]
        alternative_suffix = f"-alt-{alternative + 1}"
    suffix = {"geojson": ".geojson", "gpkg": ".gpkg", "csv": ".csv", "html": ".html", "json": ".json"}.get(format)
    if suffix is None:
        raise HTTPException(status_code=422, detail="Format: geojson, gpkg, csv, html lub json")
    path = settings.artifacts_dir / "exports" / f"{run_id}{alternative_suffix}{suffix}"
    try:
        export_run(exported_run, path, format)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FileResponse(path, filename=path.name)


frontend = Path(__file__).with_name("frontend")
if not frontend.exists():  # editable checkout; wheel contains the packaged copy
    frontend = Path(__file__).resolve().parents[2] / "frontend"
if frontend.exists():
    app.mount("/assets", StaticFiles(directory=frontend), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(frontend / "index.html")
