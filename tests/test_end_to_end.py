from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point, box, mapping

from gerry.domain import DistrictRules, JobStatus, OptimizationRequest
from gerry.elections import scenario_from_pkw
from gerry.exports import export_run
from gerry.graph import build_adjacency, validate_graph
from gerry.reconstruction import reconstruct_voronoi
from gerry.solver import ExactEnumerator


def test_reconstruction_graph_history_solver_and_export(tmp_path: Path):
    assigned = gpd.GeoDataFrame(
        {"precinct": [1, 2, 3, 4]},
        geometry=[Point(5, 5), Point(15, 5), Point(25, 5), Point(35, 5)],
        crs=2180,
    )
    polygons, reconstruction = reconstruct_voronoi(
        assigned, box(0, 0, 40, 10), expected_precincts=[1, 2, 3, 4]
    )
    polygons["key"] = polygons["precinct"].map(lambda number: f"020101_{number}")
    edges = build_adjacency(polygons, min_shared_border_m=1)
    nodes = polygons["key"].tolist()
    assert reconstruction["coverage_ratio"] == 1
    assert reconstruction["overlap_free"]
    assert validate_graph(nodes, edges) == []

    result_file = tmp_path / "pkw.csv"
    result_file.write_text(
        "TERYT,Numer obwodu,Ludność,A,B\n"
        "020101,1,100,90,10\n"
        "020101,2,100,10,90\n"
        "020101,3,100,90,10\n"
        "020101,4,100,10,90\n",
        encoding="utf-8",
    )
    scenario = scenario_from_pkw(result_file, "history", vote_columns=["A", "B"])
    geometry_by_node = {
        row.key: mapping(row.geometry)
        for row in polygons.to_crs(4326).itertuples()
    }
    request = OptimizationRequest(
        profile_id="generic-jow",
        target_kind="committee",
        target="A",
        nodes=nodes,
        edges=edges,
        scenario=scenario,
        geometry_by_node=geometry_by_node,
        rules=DistrictRules(district_count=2, seats_per_district=1, population_tolerance=0),
        alternatives=2,
    )
    run = ExactEnumerator(tmp_path / "certificates").solve(request)
    assert run.status == JobStatus.optimal
    assert run.certificate_verified
    assert run.incumbent is not None
    assert run.incumbent.target_seats == 2
    assert run.incumbent.validation.structural.value == "COMPLIANT"

    exported = export_run(run, tmp_path / "plan.geojson", "geojson")
    frame = gpd.read_file(exported)
    assert len(frame) == 4
    assert set(frame["district"]) == {0, 1}
