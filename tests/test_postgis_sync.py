import json

import geopandas as gpd
import pytest
from shapely.geometry import LineString, box

from gerry.postgis_sync import load_graph, prepare_precincts


def test_prepare_precincts_reprojects_and_normalizes_attributes():
    frame = gpd.GeoDataFrame(
        {
            "key": ["020101_1"],
            "teryt": ["020101"],
            "precinct": [1],
            "eligible": [123],
            "population": [None],
            "votes": [{"X": 10}],
            "geometry_quality": ["generated"],
        },
        geometry=[box(19.0, 52.0, 19.01, 52.01)],
        crs=4326,
    )

    prepared = prepare_precincts(frame)

    assert len(prepared) == 1
    assert prepared[0].key == "020101_1"
    assert prepared[0].number == 1
    assert prepared[0].eligible == 123
    assert prepared[0].population is None
    assert prepared[0].votes == {"X": 10}
    assert prepared[0].quality == "generated"
    assert prepared[0].geometry_wkb


def test_prepare_precincts_rejects_non_polygon_and_duplicate_keys():
    duplicate = gpd.GeoDataFrame(
        {"key": ["a", "a"], "teryt": ["1", "1"]},
        geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1)],
        crs=2180,
    )
    with pytest.raises(ValueError, match="unikalne"):
        prepare_precincts(duplicate)

    line = gpd.GeoDataFrame(
        {"key": ["a"], "teryt": ["1"]},
        geometry=[LineString([(0, 0), (1, 1)])],
        crs=2180,
    )
    with pytest.raises(ValueError, match="nie jest poligonem"):
        prepare_precincts(line)


def test_prepare_precincts_rejects_invalid_election_attributes():
    frame = gpd.GeoDataFrame(
        {
            "key": ["bad"],
            "teryt": ["123"],
            "precinct": [1],
            "votes": [{"X": -1}],
        },
        geometry=[box(0, 0, 1, 1)],
        crs=2180,
    )
    with pytest.raises(ValueError, match="TERYT"):
        prepare_precincts(frame)

    frame["teryt"] = "020101"
    with pytest.raises(ValueError, match="Głosy"):
        prepare_precincts(frame)


def test_load_graph_requires_exact_snapshot_and_node_set(tmp_path):
    path = tmp_path / "graph.json"
    path.write_text(
        json.dumps({
            "snapshot_id": "snapshot-a",
            "node_ids": ["a", "b"],
            "edges": [{
                "source": "a",
                "target": "b",
                "shared_border_m": 10,
                "kind": "physical",
            }],
        }),
        encoding="utf-8",
    )

    assert len(load_graph(path, "snapshot-a", ["b", "a"])) == 1
    with pytest.raises(ValueError, match="nie należy"):
        load_graph(path, "snapshot-b", ["a", "b"])
    with pytest.raises(ValueError, match="nie odpowiadają"):
        load_graph(path, "snapshot-a", ["a"])
