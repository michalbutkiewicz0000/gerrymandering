from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
from shapely.geometry import MultiPolygon, Polygon

from .domain import AdjacencyEdge, DataSnapshot
from .graph import validate_graph


@dataclass(frozen=True)
class PreparedPrecinct:
    key: str
    teryt: str
    number: int
    special: bool
    population: int | None
    eligible: int
    votes: dict[str, int]
    quality: str
    reconstruction: dict[str, Any]
    geometry_wkb: bytes


def _optional_int(value: Any) -> int | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return int(value)


def _non_negative_int(value: Any, *, label: str, default: int | None = None) -> int | None:
    parsed = _optional_int(value)
    if parsed is None:
        return default
    if parsed < 0:
        raise ValueError(f"{label} nie może być ujemne")
    return parsed


def _dict(value: Any) -> dict:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("votes/reconstruction must be JSON objects")
    return value


def prepare_precincts(frame: gpd.GeoDataFrame) -> list[PreparedPrecinct]:
    if frame.crs is None:
        raise ValueError("Warstwa obwodów nie ma CRS")
    required = {"key", "teryt", "geometry"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Brak kolumn warstwy obwodów: {sorted(missing)}")
    if frame.empty:
        raise ValueError("Warstwa obwodów jest pusta")
    keys = frame["key"].astype(str)
    if keys.duplicated().any():
        raise ValueError("Klucze obwodów nie są unikalne")
    metric = frame.to_crs(2180)
    prepared = []
    for position, (_, row) in enumerate(metric.iterrows(), start=1):
        geometry = row.geometry
        if geometry is None or geometry.is_empty or not geometry.is_valid:
            raise ValueError(f"Niepoprawna geometria obwodu {row['key']}")
        if isinstance(geometry, Polygon):
            geometry = MultiPolygon([geometry])
        if not isinstance(geometry, MultiPolygon):
            raise ValueError(f"Geometria obwodu {row['key']} nie jest poligonem")
        teryt = str(row["teryt"])
        if len(teryt) != 6 or not teryt.isdigit():
            raise ValueError(f"Niepoprawny kod TERYT obwodu {row['key']}: {teryt}")
        number = _non_negative_int(
            row.get("precinct", row.get("number", position)), label="Numer obwodu"
        )
        if not number:
            raise ValueError(f"Numer obwodu {row['key']} musi być dodatni")
        votes = {
            str(key): _non_negative_int(value, label=f"Głosy {key}")
            for key, value in _dict(row.get("votes")).items()
        }
        quality = str(row.get("geometry_quality", row.get("quality", "none")))
        if quality not in {"official", "generated", "approximate", "fallback", "none"}:
            raise ValueError(f"Nieznana jakość geometrii obwodu {row['key']}: {quality}")
        prepared.append(
            PreparedPrecinct(
                key=str(row["key"]),
                teryt=teryt,
                number=number,
                special=bool(row.get("special", False)),
                population=_non_negative_int(row.get("population"), label="Ludność"),
                eligible=_non_negative_int(
                    row.get("eligible"), label="Liczba uprawnionych", default=0
                ),
                votes=votes,
                quality=quality,
                reconstruction=_dict(row.get("reconstruction")),
                geometry_wkb=geometry.wkb,
            )
        )
    return prepared


def load_graph(path: Path, snapshot_id: str, node_ids: list[str]) -> list[AdjacencyEdge]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if str(payload.get("snapshot_id")) != str(snapshot_id):
        raise ValueError("Graf nie należy do synchronizowanej migawki")
    if set(map(str, payload.get("node_ids", []))) != set(node_ids):
        raise ValueError("Węzły grafu nie odpowiadają warstwie obwodów")
    edges = [AdjacencyEdge.model_validate(item) for item in payload.get("edges", [])]
    errors = validate_graph(node_ids, edges)
    if errors:
        raise ValueError("Niepoprawny graf: " + "; ".join(errors))
    return edges


def _executemany(connection, statement: str, rows: list[tuple]) -> None:
    with connection.cursor() as cursor:
        cursor.executemany(statement, rows)


def sync_snapshot_to_postgis(
    snapshot: DataSnapshot,
    frame: gpd.GeoDataFrame,
    edges: list[AdjacencyEdge],
    database_url: str,
) -> dict[str, int]:
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ValueError("Synchronizacja wymaga PostgreSQL/PostGIS")
    import psycopg
    from psycopg.types.json import Jsonb

    precincts = prepare_precincts(frame)
    node_ids = [item.key for item in precincts]
    errors = validate_graph(node_ids, edges)
    if errors:
        raise ValueError("Niepoprawny graf: " + "; ".join(errors))
    dsn = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO data_snapshots(id, election_id, effective_date, status, created_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
              election_id=EXCLUDED.election_id,
              effective_date=EXCLUDED.effective_date,
              status=EXCLUDED.status
            """,
            (
                snapshot.id,
                snapshot.election_id,
                snapshot.effective_date,
                snapshot.status,
                snapshot.created_at,
            ),
        )
        connection.execute("DELETE FROM source_artifacts WHERE snapshot_id = %s", (snapshot.id,))
        if snapshot.artifacts:
            _executemany(
                connection,
                """
                INSERT INTO source_artifacts(snapshot_id, source, url, local_path, sha256)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (snapshot.id, item.source, item.url, item.local_path, item.sha256)
                    for item in snapshot.artifacts
                ],
            )
        connection.execute("DELETE FROM adjacency_edges WHERE snapshot_id = %s", (snapshot.id,))
        connection.execute("DELETE FROM precincts WHERE snapshot_id = %s", (snapshot.id,))
        _executemany(
            connection,
            """
            INSERT INTO precincts(
              key, snapshot_id, teryt, number, special, population, eligible,
              votes, quality, reconstruction, geometry
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              ST_Multi(ST_GeomFromWKB(%s, 2180))
            )
            """,
            [
                (
                    item.key,
                    snapshot.id,
                    item.teryt,
                    item.number,
                    item.special,
                    item.population,
                    item.eligible,
                    Jsonb(item.votes),
                    item.quality,
                    Jsonb(item.reconstruction),
                    item.geometry_wkb,
                )
                for item in precincts
            ],
        )
        if edges:
            _executemany(
                connection,
                """
                INSERT INTO adjacency_edges(
                  snapshot_id, source, target, shared_border_m, kind
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (
                        snapshot.id,
                        edge.source,
                        edge.target,
                        edge.shared_border_m,
                        edge.kind,
                    )
                    for edge in edges
                ],
            )
    return {
        "snapshots": 1,
        "artifacts": len(snapshot.artifacts),
        "precincts": len(precincts),
        "edges": len(edges),
    }
