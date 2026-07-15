from __future__ import annotations

import json

import geopandas as gpd

from .domain import VoteScenario
from .elections import aggregate_scenario
from .graph import build_adjacency, dissolve_to_level, validate_graph
from .law import profile_unit_plan


# Number of leading TERYT digits that identify each scope unit the user picks.
SCOPE_PREFIX = {"wojewodztwo": 2, "powiat": 4, "gmina": 6}


def split_gmina_teryts(scenario: VoteScenario, split_population_gt: int | None) -> frozenset[str]:
    """Gmina TERYT codes a profile may keep un-aggregated because their city is large.

    The Senate provision applies to the *powiat* (a city on powiat rights, e.g.
    Warsaw): once that city's population exceeds the threshold it may be split, so
    all of its gmina-level units — the city's districts — stay their own nodes
    instead of collapsing into one powiat node.
    """
    if not split_population_gt:
        return frozenset()
    powiat_population: dict[str, int] = {}
    gminy_by_powiat: dict[str, set[str]] = {}
    for unit, value in scenario.population_by_unit.items():
        teryt = unit.split("_", 1)[0]
        powiat = teryt[:4]
        powiat_population[powiat] = powiat_population.get(powiat, 0) + value
        gminy_by_powiat.setdefault(powiat, set()).add(teryt)
    keep: set[str] = set()
    for powiat, population in powiat_population.items():
        if population > split_population_gt:
            keep |= gminy_by_powiat[powiat]
    return frozenset(keep)


def _restrict_scenario(scenario: VoteScenario, nodes: set[str]) -> VoteScenario:
    return scenario.model_copy(
        update={
            "votes_by_unit": {k: v for k, v in scenario.votes_by_unit.items() if k in nodes},
            "eligible_by_unit": {k: v for k, v in scenario.eligible_by_unit.items() if k in nodes},
            "population_by_unit": {k: v for k, v in scenario.population_by_unit.items() if k in nodes},
        }
    )


def assemble_districting_inputs(
    *,
    profile_id: str,
    scenario: VoteScenario,
    gminy: gpd.GeoDataFrame | None = None,
    precincts: gpd.GeoDataFrame | None = None,
    unit: str | None = None,
    key_column: str = "key",
    min_shared_border_m: float = 1.0,
    boundary_tolerance_m: float = 0.01,
) -> dict:
    """Build the graph, aggregated scenario and geometry a profile draws over.

    The node granularity comes from :func:`gerry.law.profile_unit_plan`: powiat or
    gmina nodes are dissolved from the national gmina boundary layer, precinct nodes
    come from a reconstructed layer for a single gmina. ``unit`` scopes generation to
    one administrative area (whole country when the profile has no ``scope_level``),
    so nationwide precinct reconstruction is never needed above the gmina council.
    """
    plan = profile_unit_plan(profile_id)
    level = plan["unit_level"]
    scope_level = plan["scope_level"]
    if scope_level:
        if not unit:
            raise ValueError(f"profil {profile_id} wymaga wskazania jednostki poziomu {scope_level}")
        if len(unit) != SCOPE_PREFIX[scope_level]:
            raise ValueError(
                f"jednostka poziomu {scope_level} ma {SCOPE_PREFIX[scope_level]} cyfr TERYT"
            )
    keep_gmina = split_gmina_teryts(scenario, plan["split_population_gt"])

    if level == "precinct":
        if precincts is None:
            raise ValueError("poziom obwodowy wymaga warstwy obwodów")
        frame = precincts.copy()
        if unit:
            frame = frame[frame["teryt"].astype(str) == unit]
        if frame.empty:
            raise ValueError("brak obwodów w zakresie wybranej jednostki")
        node_geometry = frame[[key_column, "geometry"]]
    else:
        if gminy is None:
            raise ValueError("poziom gmina/powiat wymaga warstwy granic gmin")
        frame = gminy.copy()
        if scope_level:
            prefix = SCOPE_PREFIX[scope_level]
            frame = frame[frame["teryt"].astype(str).str[:prefix] == unit]
        if frame.empty:
            raise ValueError("brak jednostek administracyjnych w zakresie")
        node_geometry = dissolve_to_level(
            frame, level, key_column=key_column, keep_gmina=keep_gmina
        )

    edges = build_adjacency(
        node_geometry,
        key_column=key_column,
        min_shared_border_m=min_shared_border_m,
        boundary_tolerance_m=boundary_tolerance_m,
    )
    node_ids = node_geometry[key_column].astype(str).tolist()
    node_set = set(node_ids)
    aggregated = _restrict_scenario(
        aggregate_scenario(scenario, level, keep_gmina=keep_gmina), node_set
    )
    container_by_node: dict[str, str] = {}
    container_level = plan["container_level"]
    if container_level:
        prefix = SCOPE_PREFIX[container_level]
        container_by_node = {node: node.split("_", 1)[0][:prefix] for node in node_ids}
    geometry = gpd.GeoDataFrame(
        {"node": node_geometry[key_column].astype(str)},
        geometry=node_geometry.geometry,
        crs=node_geometry.crs,
    ).to_crs(4326)
    graph = {
        "nodes": len(node_ids),
        "node_ids": node_ids,
        "edges": [edge.model_dump() for edge in edges],
        "errors": validate_graph(node_ids, edges),
        "build_parameters": {
            "key_column": key_column,
            "unit_level": level,
            "scope_level": scope_level,
            "unit": unit,
            "metric_crs": 2180,
            "min_shared_border_m": min_shared_border_m,
            "boundary_tolerance_m": boundary_tolerance_m,
        },
    }
    return {
        "unit_level": level,
        "scope_level": scope_level,
        "unit": unit,
        "graph": graph,
        "scenario": aggregated,
        "container_by_node": container_by_node,
        "geometry": json.loads(geometry.to_json(drop_id=True)),
    }
