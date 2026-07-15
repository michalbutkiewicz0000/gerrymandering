from __future__ import annotations

import shutil
from pathlib import Path

import geopandas as gpd

from .domain import DistrictRules, OptimizationRequest
from .elections import scenario_from_pkw
from .exports import export_run
from .graph import build_adjacency, validate_graph
from .pipeline import NationalReconstructionPipeline
from .solver import ExactEnumerator
from .sources import load_registry


def run_real_smoke(source: Path, workspace: Path, teryt: str = "020302") -> dict:
    """Exercise the complete local pipeline on immutable mapa_obwodow inputs."""
    data = source / "data" if (source / "data").is_dir() else source
    registry_path = data / "metadata/obwody_glosowania_utf8.xlsx"
    boundaries_path = data / "raw/gminy_boundaries.json"
    prg_path = data / f"raw/prg/{teryt}.parquet"
    results_path = data / "raw/sejm2023/results.zip"
    missing = [
        str(path) for path in (
            registry_path, boundaries_path, prg_path, results_path
        ) if not path.is_file()
    ]
    if missing:
        raise FileNotFoundError(f"Brak danych real-smoke: {', '.join(missing)}")

    cache = workspace / "raw/prg"
    cache.mkdir(parents=True, exist_ok=True)
    shutil.copy2(prg_path, cache / prg_path.name)
    registry = load_registry(registry_path)
    boundaries = gpd.read_file(boundaries_path).to_crs(4326)
    if "teryt" not in boundaries and "JPT_KOD_JE" in boundaries:
        boundaries["teryt"] = boundaries["JPT_KOD_JE"].astype(str).str[:6]
    if "teryt" not in boundaries:
        raise ValueError("Warstwa granic nie zawiera TERYT ani JPT_KOD_JE")
    selected = boundaries.loc[boundaries.teryt.astype(str) == teryt]
    if selected.empty:
        raise ValueError(f"Brak granicy gminy {teryt}")

    pipeline = NationalReconstructionPipeline(
        workspace, snapshot_id=f"real-{teryt}"
    )
    polygons, report = pipeline.reconstruct_gmina(
        teryt, registry, selected.geometry.union_all(), force=True
    )
    nodes = polygons.key.astype(str).tolist()
    if len(nodes) > 14:
        raise ValueError(
            f"Real-smoke używa enumeratora (limit 14), a {teryt} ma {len(nodes)} obwodów"
        )
    edges = build_adjacency(polygons)
    graph_errors = validate_graph(nodes, edges)
    if graph_errors:
        raise ValueError(f"Nieprawidłowy graf rzeczywisty: {graph_errors}")

    scenario = scenario_from_pkw(
        results_path,
        f"Sejm 2023 — real smoke {teryt}",
        attachments_path=pipeline.report_dir / f"{teryt}_special.json",
    )
    missing_results = sorted(set(nodes) - set(scenario.votes_by_unit))
    if missing_results:
        raise ValueError(f"Brak wyników PKW dla: {missing_results}")
    scenario.votes_by_unit = {
        node: scenario.votes_by_unit[node] for node in nodes
    }
    scenario.eligible_by_unit = {
        node: scenario.eligible_by_unit.get(node, 0) for node in nodes
    }
    # The Sejm result archive has eligible voters but no official population.
    # This is explicitly an analytical balance proxy, not a statutory norm.
    scenario.population_by_unit = dict(scenario.eligible_by_unit)
    parties = sorted({
        party for votes in scenario.votes_by_unit.values() for party in votes
    })
    target = max(
        parties,
        key=lambda party: sum(
            votes.get(party, 0) for votes in scenario.votes_by_unit.values()
        ),
    )
    request = OptimizationRequest(
        profile_id="generic-jow",
        target_kind="committee",
        target=target,
        scenario=scenario,
        rules=DistrictRules(district_count=2, population_tolerance=0.75),
        nodes=nodes,
        edges=edges,
        geometry_by_node={
            row.key: row.geometry.__geo_interface__ for row in polygons.itertuples()
        },
        alternatives=3,
    )
    run = ExactEnumerator(workspace / "artifacts/real-smoke-certificates").solve(
        request
    )
    if run.status.value != "OPTIMAL" or not run.certificate_verified:
        raise RuntimeError(f"Real-smoke nie dowiódł optimum: {run.status.value}")
    export_path = export_run(
        run, workspace / f"artifacts/real-smoke-{teryt}.geojson", "geojson"
    )
    expected = int(
        registry[(registry.teryt == teryt) & ~registry.special].precinct.nunique()
    )
    return {
        "teryt": teryt,
        "precincts": len(polygons),
        "expected_precincts": expected,
        "assignment_rate": report["assignment_rate"],
        "special_attached": report["special_attached"],
        "edges": len(edges),
        "graph_errors": graph_errors,
        "scenario_units": len(scenario.votes_by_unit),
        "committees": len(parties),
        "target": target,
        "status": run.status.value,
        "certificate_verified": run.certificate_verified,
        "alternatives": len(run.alternatives),
        "export": str(export_path),
        "population_basis": "eligible_voters_analytical_proxy",
    }
