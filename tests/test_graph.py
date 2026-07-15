import geopandas as gpd
import pytest
from shapely.geometry import GeometryCollection, LineString, box

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


def test_rook_adjacency_repairs_subcentimeter_parallel_boundary_gap():
    frame = gpd.GeoDataFrame(
        {"key": ["a", "b", "corner"]},
        geometry=[
            box(0, 0, 10, 10),
            box(10.005, 0, 20, 10),
            box(20.005, 10, 30, 20),
        ],
        crs=2180,
    )

    edges = build_adjacency(frame, boundary_tolerance_m=0.01)

    assert len(edges) == 1
    assert (edges[0].source, edges[0].target) == ("a", "b")
    assert edges[0].shared_border_m == pytest.approx(10.0, abs=0.01)


def test_rook_adjacency_can_disable_boundary_tolerance():
    frame = gpd.GeoDataFrame(
        {"key": ["a", "b"]},
        geometry=[box(0, 0, 10, 10), box(10.005, 0, 20, 10)],
        crs=2180,
    )

    assert build_adjacency(frame, boundary_tolerance_m=0) == []


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


def test_graph_rejects_valid_but_non_polygonal_geometry():
    frame = gpd.GeoDataFrame(
        {"key": ["a"]},
        geometry=[GeometryCollection([box(0, 0, 1, 1), LineString([(0, 0), (1, 1)])])],
        crs=2180,
    )

    with pytest.raises(ValueError, match="must be polygonal"):
        build_adjacency(frame)
