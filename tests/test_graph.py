import geopandas as gpd
import pytest
from shapely.geometry import box

from gerry.domain import AdjacencyEdge
from gerry.graph import build_adjacency, contract, cut_border, validate_graph


def test_rook_adjacency_ignores_corner_contact():
    frame = gpd.GeoDataFrame(
        {"key": ["a", "b", "c"]},
        geometry=[box(0, 0, 10, 10), box(10, 0, 20, 10), box(20, 10, 30, 20)],
        crs=2180,
    )
    edges = build_adjacency(frame)
    assert [(edge.source, edge.target, edge.shared_border_m) for edge in edges] == [("a", "b", 10.0)]


def test_graph_contract_and_cut_border():
    edges = [
        AdjacencyEdge(source="a", target="b", shared_border_m=2),
        AdjacencyEdge(source="b", target="c", shared_border_m=3),
    ]
    assert cut_border({"a": 0, "b": 0, "c": 1}, edges) == 3
    contracted = contract(["a", "b", "c"], edges, {"a": "x", "b": "x", "c": "y"})
    assert contracted == [AdjacencyEdge(source="x", target="y", shared_border_m=3)]
    assert not validate_graph(["a", "b", "c"], edges)


def test_graph_rejects_duplicate_node_keys():
    frame = gpd.GeoDataFrame(
        {"key": ["a", "a"]},
        geometry=[box(0, 0, 10, 10), box(10, 0, 20, 10)],
        crs=2180,
    )

    with pytest.raises(ValueError, match="must be unique"):
        build_adjacency(frame)

    assert validate_graph(["a", "a"], []) == ["duplicate nodes: ['a']"]
