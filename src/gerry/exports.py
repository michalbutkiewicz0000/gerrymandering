from __future__ import annotations

import csv
import html
import json
from pathlib import Path

import geopandas as gpd
from shapely.geometry import shape

from .domain import OptimizationRun


def plan_frame(run: OptimizationRun) -> gpd.GeoDataFrame:
    if run.incumbent is None:
        raise ValueError("Zadanie nie zawiera planu")
    rows = []
    for node, district in run.incumbent.assignment.items():
        geometry = run.request.geometry_by_node.get(node)
        rows.append({
            "node": node,
            "district": district,
            "target_seats": run.incumbent.seats_by_district.get(district, {}).get(run.request.target, 0),
            "geometry": shape(geometry) if geometry else None,
        })
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=4326)


def export_run(run: OptimizationRun, output: Path, format: str) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.part{output.suffix}")
    temporary.unlink(missing_ok=True)
    try:
        if format == "json":
            temporary.write_text(run.model_dump_json(indent=2), encoding="utf-8")
        elif format == "csv":
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["node", "district"])
                writer.writerows(sorted(run.incumbent.assignment.items()))
        elif format == "geojson":
            frame = plan_frame(run)
            if frame.geometry.notna().any():
                frame.to_file(temporary, driver="GeoJSON")
            else:
                features = [
                    {
                        "type": "Feature", "geometry": None,
                        "properties": {"node": row.node, "district": row.district},
                    }
                    for row in frame.itertuples()
                ]
                temporary.write_text(
                    json.dumps({"type": "FeatureCollection", "features": features}),
                    encoding="utf-8",
                )
        elif format == "gpkg":
            frame = plan_frame(run)
            if not frame.geometry.notna().any():
                raise ValueError("Eksport GPKG wymaga geometry_by_node")
            frame.to_file(temporary, layer="plan", driver="GPKG")
        elif format == "html":
            validation = run.incumbent.validation
            findings = "".join(
                f"<tr><td>{html.escape(item.code)}</td><td>{item.status.value}</td>"
                f"<td>{html.escape(item.message)}</td></tr>"
                for item in (validation.findings if validation else [])
            )
            temporary.write_text(
                "<!doctype html><meta charset='utf-8'><title>Raport podziału</title>"
                f"<h1>Raport {run.id}</h1><p>Status: <strong>{run.status.value}</strong></p>"
                f"<p>Cel: {html.escape(run.request.target)}, mandaty: {run.incumbent.target_seats}</p>"
                f"<p>Certyfikat: {'zweryfikowany' if run.certificate_verified else 'NIECERTYFIKOWANY'}</p>"
                "<table><thead><tr><th>Reguła</th><th>Status</th><th>Opis</th></tr></thead>"
                f"<tbody>{findings}</tbody></table>",
                encoding="utf-8",
            )
        else:
            raise ValueError(f"Nieobsługiwany format: {format}")
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return output
