from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

import geopandas as gpd
import networkx as nx

from .domain import AdjacencyEdge


def build_adjacency(
    geometries: gpd.GeoDataFrame,
    *,
    key_column: str = "key",
    min_shared_border_m: float = 1.0,
    metric_crs: int = 2180,
) -> list[AdjacencyEdge]:
    if geometries.crs is None:
        raise ValueError("input geometries must have a CRS")
    if key_column not in geometries:
        raise ValueError(f"missing graph key column: {key_column}")
    if geometries[key_column].isna().any():
        raise ValueError("graph node keys must not be null")
    keys = geometries[key_column].astype(str)
    if keys.duplicated().any():
        duplicates = sorted(keys[keys.duplicated(keep=False)].unique())
        raise ValueError(f"graph node keys must be unique: {duplicates}")
    if geometries.geometry.isna().any() or geometries.geometry.is_empty.any():
        raise ValueError("graph geometries must not be null or empty")
    if (~geometries.geometry.is_valid).any():
        raise ValueError("graph geometries must be valid")
    if min_shared_border_m < 0:
        raise ValueError("minimum shared border must be non-negative")
    frame = geometries[[key_column, "geometry"]].to_crs(epsg=metric_crs).reset_index(drop=True)
    spatial_index = frame.sindex
    edges: list[AdjacencyEdge] = []
    for left_idx, left in frame.iterrows():
        candidates = spatial_index.query(left.geometry, predicate="intersects")
        for right_idx in candidates:
            if int(right_idx) <= left_idx:
                continue
            right = frame.iloc[int(right_idx)]
            shared = left.geometry.boundary.intersection(right.geometry.boundary)
            length = float(shared.length)
            if length >= min_shared_border_m:
                edges.append(
                    AdjacencyEdge(
                        source=str(left[key_column]),
                        target=str(right[key_column]),
                        shared_border_m=round(length, 3),
                    )
                )
    return sorted(edges, key=lambda edge: (edge.source, edge.target))


def as_networkx(nodes: Iterable[str], edges: Iterable[AdjacencyEdge], allowed_kinds=None) -> nx.Graph:
    allowed = set(allowed_kinds or {"physical"})
    graph = nx.Graph()
    graph.add_nodes_from(nodes)
    for edge in edges:
        if edge.kind in allowed:
            graph.add_edge(edge.source, edge.target, shared_border_m=edge.shared_border_m, kind=edge.kind)
    return graph


def validate_graph(nodes: Iterable[str], edges: Iterable[AdjacencyEdge]) -> list[str]:
    node_list = list(nodes)
    node_set = set(node_list)
    seen: set[tuple[str, str]] = set()
    errors: list[str] = []
    if len(node_list) != len(node_set):
        duplicates = sorted({node for node in node_list if node_list.count(node) > 1})
        errors.append(f"duplicate nodes: {duplicates}")
    for edge in edges:
        pair = tuple(sorted((edge.source, edge.target)))
        if edge.source not in node_set or edge.target not in node_set:
            errors.append(f"edge references unknown node: {pair}")
        if pair in seen:
            errors.append(f"duplicate edge: {pair}")
        seen.add(pair)
    graph = as_networkx(node_set, edges, {"physical", "bridge", "ferry"})
    if node_set and not nx.is_connected(graph):
        errors.append(f"graph has {nx.number_connected_components(graph)} components")
    return errors


def cut_border(assignment: dict[str, int], edges: Iterable[AdjacencyEdge]) -> float:
    return sum(
        edge.shared_border_m
        for edge in edges
        if assignment.get(edge.source) != assignment.get(edge.target)
    )


def contract(
    nodes: Iterable[str], edges: Iterable[AdjacencyEdge], parent_by_node: dict[str, str]
) -> list[AdjacencyEdge]:
    lengths: defaultdict[tuple[str, str], float] = defaultdict(float)
    for edge in edges:
        left = parent_by_node[edge.source]
        right = parent_by_node[edge.target]
        if left == right:
            continue
        lengths[tuple(sorted((left, right)))] += edge.shared_border_m
    return [
        AdjacencyEdge(source=left, target=right, shared_border_m=round(length, 3))
        for (left, right), length in sorted(lengths.items())
    ]
