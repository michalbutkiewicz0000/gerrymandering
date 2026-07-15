from __future__ import annotations

import json
import shutil
import time
from datetime import date
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import geopandas as gpd
import typer

from .domain import OptimizationRequest
from .elections import scenario_from_pkw
from .graph import build_adjacency, validate_graph
from .exports import export_run
from .pipeline import NationalReconstructionPipeline
from .settings import settings
from .snapshots import SnapshotStore


app = typer.Typer(no_args_is_help=True, help="Gerrymandering PL — pipeline i solver")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _read_geodata(path: Path) -> gpd.GeoDataFrame:
    return gpd.read_parquet(path) if path.suffix.lower() in {".parquet", ".pq"} else gpd.read_file(path)


@app.command("doctor")
def doctor() -> None:
    import shutil

    from .law_archive import verify_law_archive
    from .scip_solver import exact_scip_available

    settings.ensure_dirs()
    exact, detail = exact_scip_available()
    viprcomp = shutil.which("viprcomp") is not None
    viprchk = shutil.which("viprchk") is not None
    certified = exact and viprcomp and viprchk
    law_archive_valid, law_archive_detail = verify_law_archive()
    typer.echo(
        f"Python i konfiguracja: OK\nDane: {settings.data_dir.resolve()}\n"
        f"Prawo: {settings.law_snapshot}\nSolver exact: {'OK' if exact else 'NIEDOSTĘPNY'} ({detail})\n"
        f"VIPR: {'OK' if viprcomp and viprchk else 'NIEDOSTĘPNY'} "
        f"(viprcomp={'tak' if viprcomp else 'nie'}, viprchk={'tak' if viprchk else 'nie'})\n"
        f"Certyfikacja dużych zadań: {'OK' if certified else 'NIEDOSTĘPNA'}\n"
        f"Archiwum prawa: {'OK' if law_archive_valid else 'BŁĄD'} ({law_archive_detail})"
    )


@app.command("law-archive")
def law_archive_command(
    output: Annotated[
        Path,
        typer.Argument(help="Katalog docelowy PDF-ów i manifestu SHA-256"),
    ] = Path("src/gerry/resources/legal"),
) -> None:
    """Download the frozen official ELI documents into a verifiable archive."""
    from .law_archive import archive_law_sources, verify_law_archive

    manifest = archive_law_sources(output)
    valid, detail = verify_law_archive(output)
    if not valid:
        raise typer.BadParameter(detail)
    typer.echo(f"{detail}; manifest: {output / 'manifest.json'}; pliki: {len(manifest['documents'])}")


@app.command("law-verify")
def law_verify(
    archive: Annotated[
        Path | None,
        typer.Argument(help="Opcjonalny katalog archiwum; domyślnie zasób pakietu"),
    ] = None,
) -> None:
    """Verify hashes and source bindings of the frozen legal archive."""
    from .law_archive import verify_law_archive

    valid, detail = verify_law_archive(archive)
    typer.echo(detail)
    if not valid:
        raise typer.Exit(1)


@app.command("solver-smoke")
def solver_smoke() -> None:
    """Run SCIP+VIPR end to end; intended for the exact Docker image and CI."""
    from .scip_solver import ScipExactSolver

    example = files("gerry").joinpath("resources/small_request.json")
    if not example.is_file():
        example = Path(__file__).resolve().parents[2] / "examples" / "small_request.json"
    request = OptimizationRequest.model_validate_json(example.read_text(encoding="utf-8"))
    request.alternatives = 1
    run = ScipExactSolver(settings.artifacts_dir / "smoke").solve(request)
    typer.echo(
        f"Status: {run.status.value}; certyfikat: "
        f"{'zweryfikowany' if run.certificate_verified else 'NIEZWERYFIKOWANY'}"
    )
    if run.status.value != "OPTIMAL" or not run.certificate_verified:
        if run.message:
            typer.echo(run.message, err=True)
        raise typer.Exit(1)

    manifest = json.loads(Path(run.certificate_path).read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != 2
        or not manifest.get("request_sha256")
        or not manifest.get("proofs")
        or any(
            not record.get("model_sha256") or not record.get("proof_sha256")
            for record in manifest["proofs"]
        )
    ):
        typer.echo("Manifest nie wiąże wszystkich dowodów z modelami CIP.", err=True)
        raise typer.Exit(1)

    infeasible_request = request.model_copy(deep=True)
    infeasible_request.rules.district_count = len(request.nodes) + 1
    infeasible = ScipExactSolver(settings.artifacts_dir / "smoke-infeasible").solve(
        infeasible_request
    )
    typer.echo(
        f"Brak rozwiązania: {infeasible.status.value}; certyfikat: "
        f"{'zweryfikowany' if infeasible.certificate_verified else 'NIEZWERYFIKOWANY'}"
    )
    if infeasible.status.value != "INFEASIBLE" or not infeasible.certificate_verified:
        if infeasible.message:
            typer.echo(infeasible.message, err=True)
        raise typer.Exit(1)
    infeasible_manifest = json.loads(
        Path(infeasible.certificate_path).read_text(encoding="utf-8")
    )
    if infeasible_manifest.get("schema_version") != 2:
        typer.echo("Manifest dowodu niewykonalności ma nieaktualny schemat.", err=True)
        raise typer.Exit(1)


@app.command("real-smoke")
def real_smoke(
    source: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    teryt: str = "020302",
) -> None:
    """Run reconstruction→graph→PKW→solver→export on real local inputs."""
    from .real_smoke import run_real_smoke

    result = run_real_smoke(
        source, settings.artifacts_dir / "real-smoke-workspace", teryt
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("certificate-verify")
def certificate_verify(
    manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Recheck hashes and every completed VIPR proof in a persisted manifest."""
    from .scip_solver import verify_vipr_manifest

    verified, detail = verify_vipr_manifest(manifest, rerun_viprchk=True)
    typer.echo(f"{'ZWERYFIKOWANY' if verified else 'NIEPRAWIDŁOWY'}: {detail}")
    if not verified:
        raise typer.Exit(1)


@app.command("migrate")
def migrate() -> None:
    """Apply the idempotent PostGIS schema used by Docker deployment."""
    if settings.database_url.startswith("sqlite"):
        from .db import create_schema
        create_schema()
    else:
        import psycopg
        resources = files("gerry").joinpath("resources")
        migrations = sorted(
            item for item in resources.iterdir()
            if item.name.endswith(".sql")
        )
        dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(dsn) as connection:
            # API and worker start concurrently in Compose. Serialize DDL for
            # this database for the duration of the migration transaction.
            connection.execute("SELECT pg_advisory_xact_lock(1737279259)")
            for migration in migrations:
                connection.execute(migration.read_text(encoding="utf-8"))
    typer.echo("Schemat bazy jest aktualny.")


@app.command("snapshot-create")
def snapshot_create(election_id: str, effective_date: str) -> None:
    try:
        parsed_date = date.fromisoformat(effective_date)
    except ValueError as exc:
        raise typer.BadParameter("Data musi mieć format RRRR-MM-DD") from exc
    snapshot = SnapshotStore(settings.raw_dir / "snapshots").create(election_id, parsed_date)
    typer.echo(snapshot.model_dump_json(indent=2))


@app.command("import-mapa-obwodow")
def import_mapa_obwodow(
    source: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    election: str = "sejm2023",
) -> None:
    """Copy only immutable inputs from the sibling project, never its dirty generated outputs."""
    import_archive = settings.raw_dir / "imports" / "mapa_obwodow" / election
    import_archive.mkdir(parents=True, exist_ok=True)
    candidates = [
        (
            source / "data" / "metadata" / "obwody_glosowania_utf8.xlsx",
            import_archive / "obwody_glosowania_utf8.xlsx",
        ),
        (source / "data" / "raw" / election, settings.raw_dir / "elections" / election),
        (source / "data" / "raw" / "prg", settings.raw_dir / "prg"),
        (
            source / "data" / "raw" / "gminy_boundaries.json",
            import_archive / "gminy_boundaries.json",
        ),
    ]
    copied = 0
    for item, target in candidates:
        if not item.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
            copied += sum(path.is_file() for path in target.rglob("*"))
        else:
            shutil.copy2(item, target)
            copied += 1
    boundary_source = source / "data" / "raw" / "gminy_boundaries.json"
    if boundary_source.exists():
        boundary_frame = gpd.read_file(boundary_source)
        if "teryt" not in boundary_frame and "JPT_KOD_JE" in boundary_frame:
            boundary_frame["teryt"] = boundary_frame["JPT_KOD_JE"].astype(str).str[:6]
        if "teryt" in boundary_frame:
            boundary_cache = settings.raw_dir / "prg_boundaries" / "gminy.parquet"
            boundary_cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = boundary_cache.with_name("gminy.tmp.parquet")
            boundary_frame[["teryt", "geometry"]].to_parquet(temporary, index=False)
            temporary.replace(boundary_cache)
            copied += 1
    typer.echo(f"Zaimportowano {copied} surowych artefaktów; archiwum: {import_archive}")


@app.command("graph-build")
def graph_build(
    source: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Path,
    snapshot_id: Annotated[str, typer.Option(help="Identyfikator migawki danych")],
    key_column: str = "key",
    min_border_m: float = 1.0,
    boundary_tolerance_m: float = 0.01,
    unit_level: Annotated[
        str,
        typer.Option(
            help="Poziom węzła: precinct (obwody), gmina lub powiat. "
            "Dla gmina/powiat warstwa jest zlewana po kolumnie teryt."
        ),
    ] = "precinct",
    keep_gmina: Annotated[
        str,
        typer.Option(
            help="Kody TERYT gmin pozostawiane osobno na poziomie powiatu "
            "(np. dzielnice Warszawy dla Senatu), rozdzielone przecinkami"
        ),
    ] = "",
) -> None:
    from .sources import UNIT_LEVELS

    if unit_level not in UNIT_LEVELS:
        raise typer.BadParameter(f"unit-level musi być jednym z: {', '.join(UNIT_LEVELS)}")
    if SnapshotStore(settings.raw_dir / "snapshots").get(snapshot_id) is None:
        raise typer.BadParameter("Nie znaleziono migawki; najpierw użyj snapshot-create")
    snapshot_root = (settings.processed_dir / "snapshots" / snapshot_id).resolve()
    resolved_source = source.resolve()
    if not resolved_source.is_relative_to(snapshot_root):
        raise typer.BadParameter("Warstwa wejściowa musi należeć do wskazanej migawki")
    resolved_output = output.resolve()
    if not resolved_output.is_relative_to(snapshot_root):
        raise typer.BadParameter("Graf wynikowy musi należeć do wskazanej migawki")
    frame = _read_geodata(source)
    if unit_level != "precinct":
        from .graph import dissolve_to_level

        if "teryt" not in frame:
            raise typer.BadParameter("Zlewanie do gmina/powiat wymaga kolumny teryt w warstwie")
        frame = dissolve_to_level(
            frame,
            unit_level,
            key_column=key_column,
            keep_gmina=frozenset(
                value.strip() for value in keep_gmina.split(",") if value.strip()
            ),
        )
    try:
        edges = build_adjacency(
            frame,
            key_column=key_column,
            min_shared_border_m=min_border_m,
            boundary_tolerance_m=boundary_tolerance_m,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    errors = validate_graph(frame[key_column].astype(str), edges)
    payload = {
        "snapshot_id": snapshot_id,
        "nodes": len(frame),
        "node_ids": frame[key_column].astype(str).tolist(),
        "edges": [edge.model_dump() for edge in edges],
        "errors": errors,
        "build_parameters": {
            "key_column": key_column,
            "unit_level": unit_level,
            "metric_crs": 2180,
            "min_shared_border_m": min_border_m,
            "boundary_tolerance_m": boundary_tolerance_m,
        },
    }
    _atomic_write_text(
        resolved_output, json.dumps(payload, ensure_ascii=False, indent=2)
    )
    typer.echo(
        f"Krawędzie: {len(edges)}; problemy: {len(errors)}; zapisano {resolved_output}"
    )


@app.command("reconstruct")
def reconstruct(
    registry: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    snapshot_id: Annotated[str, typer.Option(help="Identyfikator utworzonej migawki danych")],
    boundaries: Annotated[Path | None, typer.Option(help="Opcjonalna warstwa granic; bez niej pobierany jest PRG WFS")] = None,
    limit: int | None = None,
    teryt: Annotated[
        str,
        typer.Option(help="Opcjonalna lista kodów TERYT rozdzielona przecinkami"),
    ] = "",
    retry_failed: Annotated[
        bool, typer.Option(help="Ponów wyłącznie błędy z poprzedniego national.json")
    ] = False,
    force: Annotated[bool, typer.Option(help="Przelicz także istniejący cache")] = False,
    workers: Annotated[
        int,
        typer.Option(help="Liczba równoległych workerów; 0 = wszystkie logiczne CPU"),
    ] = 0,
) -> None:
    if SnapshotStore(settings.raw_dir / "snapshots").get(snapshot_id) is None:
        raise typer.BadParameter("Nie znaleziono migawki; najpierw użyj snapshot-create")
    boundary_frame = _read_geodata(boundaries) if boundaries else None
    if boundary_frame is not None and "teryt" not in boundary_frame:
        raise typer.BadParameter("Warstwa granic musi zawierać kolumnę teryt")

    def report_progress(completed: int, total: int, report: dict | None) -> None:
        if report is None:
            typer.echo(f"Cache: {completed}/{total}")
        elif completed == total or completed % 25 == 0 or "error" in report:
            detail = f"; BŁĄD: {report['error']}" if "error" in report else ""
            typer.echo(
                f"Postęp: {completed}/{total}; TERYT {report['teryt']}{detail}"
            )

    reports = NationalReconstructionPipeline(
        settings.data_dir, snapshot_id=snapshot_id
    ).run(
        registry,
        boundary_frame,
        limit=limit,
        teryts=[value.strip() for value in teryt.split(",") if value.strip()] or None,
        retry_failed=retry_failed,
        force=force,
        workers=workers,
        progress_callback=report_progress,
    )
    failed = sum("error" in report for report in reports)
    typer.echo(f"Przetworzono {len(reports)} gmin; błędy: {failed}")
    if failed:
        raise typer.Exit(1)


@app.command("postgis-sync")
def postgis_sync(
    snapshot_id: Annotated[str, typer.Argument(help="Identyfikator migawki danych")],
    source: Annotated[
        Path | None,
        typer.Option(help="Warstwa obwodów; domyślnie precincts.gpkg migawki"),
    ] = None,
    graph: Annotated[
        Path | None,
        typer.Option(help="Graf JSON; domyślnie graph.json migawki"),
    ] = None,
) -> None:
    """Transactionally synchronize a reconstructed snapshot and graph to PostGIS."""
    from .postgis_sync import load_graph, prepare_precincts, sync_snapshot_to_postgis

    store = SnapshotStore(settings.raw_dir / "snapshots")
    snapshot = store.get(snapshot_id)
    if snapshot is None:
        raise typer.BadParameter("Nie znaleziono migawki")
    root = (settings.processed_dir / "snapshots" / snapshot_id).resolve()
    source_path = (source or root / "precincts.gpkg").resolve()
    graph_path = (graph or root / "graph.json").resolve()
    if not source_path.is_relative_to(root) or not graph_path.is_relative_to(root):
        raise typer.BadParameter("Warstwa i graf muszą należeć do wskazanej migawki")
    if not source_path.is_file() or not graph_path.is_file():
        raise typer.BadParameter("Brak warstwy precincts.gpkg albo graph.json")
    try:
        frame = gpd.read_file(source_path)
        prepared = prepare_precincts(frame)
        edges = load_graph(graph_path, snapshot_id, [item.key for item in prepared])
        result = sync_snapshot_to_postgis(
            snapshot, frame, edges, settings.database_url
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        "Zsynchronizowano PostGIS: "
        f"migawki={result['snapshots']}, artefakty={result['artifacts']}, "
        f"obwody={result['precincts']}, krawędzie={result['edges']}"
    )


@app.command("optimize")
def optimize(
    request_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Path | None = None,
) -> None:
    from .services import optimization_service

    request = OptimizationRequest.model_validate_json(request_file.read_text(encoding="utf-8"))
    try:
        run = optimization_service.solve_now(request)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = run.model_dump_json(indent=2)
    if output:
        _atomic_write_text(output, payload)
    typer.echo(payload)
    if run.status.value not in {"OPTIMAL", "INFEASIBLE", "OBJECTIVE_INVARIANT"}:
        raise typer.Exit(1)


@app.command("scenario-import")
def scenario_import(
    results: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    name: str,
    output: Path,
    attachments: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    thresholds_json: str = "{}",
    exempt: str = "",
    vote_columns: str = "",
    snapshot: Annotated[
        str | None,
        typer.Option(help="Powiąż scenariusz z migawką i udostępnij go w API/interfejsie"),
    ] = None,
) -> None:
    """Import a historical wide PKW result sheet; repeat for each election.

    With --snapshot the scenario is bound to that snapshot, aligned to its graph
    nodes and registered so the API and the web wizard list it automatically.
    """
    try:
        thresholds = json.loads(thresholds_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("thresholds-json musi być obiektem JSON") from exc
    scenario = scenario_from_pkw(
        results, name, attachments_path=attachments,
        vote_columns=[item.strip() for item in vote_columns.split(",") if item.strip()] or None,
        thresholds=thresholds,
        threshold_exempt={item.strip() for item in exempt.split(",") if item.strip()},
    )
    if snapshot is not None:
        _bind_scenario_to_snapshot(scenario, snapshot)
    _atomic_write_text(output, scenario.model_dump_json(indent=2))
    typer.echo(f"Jednostki: {len(scenario.votes_by_unit)}; zapisano {output}")
    if snapshot is not None:
        registered = _register_scenario(scenario)
        typer.echo(f"Zarejestrowano scenariusz {scenario.id} dla migawki {snapshot}: {registered}")


def _bind_scenario_to_snapshot(scenario, snapshot_id: str) -> None:
    """Set the snapshot and drop vote units that are not nodes of its graph."""
    from uuid import UUID

    if SnapshotStore(settings.raw_dir / "snapshots").get(snapshot_id) is None:
        raise typer.BadParameter("Nieznana migawka")
    graph_path = settings.processed_dir / "snapshots" / snapshot_id / "graph.json"
    if not graph_path.exists():
        raise typer.BadParameter("Migawka nie ma zbudowanego grafu (uruchom graph-build)")
    nodes = {str(node) for node in json.loads(graph_path.read_text(encoding="utf-8"))["node_ids"]}
    missing = sorted(nodes - set(scenario.votes_by_unit))
    if missing:
        raise typer.BadParameter(f"Brak wyników PKW dla {len(missing)} obwodów, np. {missing[:5]}")
    scenario.snapshot_id = UUID(snapshot_id)
    scenario.votes_by_unit = {node: scenario.votes_by_unit[node] for node in nodes}
    scenario.eligible_by_unit = {k: v for k, v in scenario.eligible_by_unit.items() if k in nodes}
    scenario.population_by_unit = {k: v for k, v in scenario.population_by_unit.items() if k in nodes}


def _register_scenario(scenario) -> Path:
    """Write the scenario and its summary sidecar into the artifacts store."""
    root = settings.artifacts_dir / "scenarios"
    committees = {c for votes in scenario.votes_by_unit.values() for c in votes}
    summary = {
        "id": str(scenario.id),
        "name": scenario.name,
        "snapshot_id": str(scenario.snapshot_id) if scenario.snapshot_id else None,
        "unit_count": len(scenario.votes_by_unit),
        "committee_count": len(committees),
    }
    _atomic_write_text(root / f"{scenario.id}.json", scenario.model_dump_json(indent=2))
    _atomic_write_text(root / "metadata" / f"{scenario.id}.json", json.dumps(summary, indent=2))
    return root / f"{scenario.id}.json"


@app.command("export")
def export_command(run_id: str, output: Path, format: str = "geojson") -> None:
    from uuid import UUID

    from .services import optimization_service

    run = optimization_service.repository.get(UUID(run_id))
    if run is None:
        raise typer.BadParameter("Nie znaleziono zadania")
    export_run(run, output, format)
    typer.echo(f"Zapisano {output}")


@app.command("worker")
def worker() -> None:
    from .services import optimization_service

    typer.echo("Worker gotowy; obserwuję trwałą kolejkę zadań.")
    while True:
        processed = optimization_service.process_queued()
        if processed:
            typer.echo(f"Przetworzono zadań: {processed}")
        time.sleep(5)


if __name__ == "__main__":
    app()
