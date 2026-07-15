import geopandas as gpd
from shapely.geometry import Point, box

from gerry.reconstruction import (
    Parity,
    assign_addresses,
    normalize_text,
    parse_boundary_description,
    reconstruct_voronoi,
)
from gerry.sources import load_registry


def test_normalize_and_parse_parity_regression():
    assert normalize_text("Aleja Józefa Piłsudskiego") == "jozefa pilsudskiego"
    rules = parse_boundary_description(1, "miasto", "ul. Testowa: 1-9 nieparzyste")
    assert rules.rules[0].parity == Parity.odd
    assert rules.rules[0].matches("Testowa", "5")
    assert not rules.rules[0].matches("Testowa", "6")


def test_assign_addresses_prefers_specific_rule():
    addresses = gpd.GeoDataFrame(
        {"street": ["Testowa"], "number": ["5"], "village": [""]},
        geometry=[Point(1, 1)], crs=2180,
    )
    broad = parse_boundary_description(1, "miasto", "ul. Testowa")
    specific = parse_boundary_description(2, "miasto", "ul. Testowa: 1-9 nieparzyste")
    assigned = assign_addresses(addresses, [broad, specific])
    assert assigned.iloc[0]["precinct"] == 2


def test_reconstruction_is_gap_free_and_adds_missing_seed():
    addresses = gpd.GeoDataFrame(
        {"precinct": [1, 1]}, geometry=[Point(2, 2), Point(3, 3)], crs=2180
    )
    polygons, report = reconstruct_voronoi(
        addresses, box(0, 0, 10, 10), expected_precincts=[1, 2], fallback_points={2: Point(8, 8)}
    )
    assert set(polygons["precinct"]) == {1, 2}
    assert report["coverage_ratio"] == 1.0
    assert report["overlap_free"]
    assert report["fallback_precincts"] == [2]


def test_registry_imports_official_population_column(tmp_path):
    registry = tmp_path / "registry.csv"
    registry.write_text(
        "TERYT gminy,Numer,Opis granic,Typ obszaru,Mieszkańcy,Wyborcy\n"
        "020101,1,Testowa,miasto,1200,900\n",
        encoding="utf-8",
    )
    frame = load_registry(registry)
    assert frame.iloc[0].population == 1200
    assert frame.iloc[0].eligible == 900
