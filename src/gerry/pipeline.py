from __future__ import annotations

import json
import math
import multiprocessing
import os
import re
from functools import wraps
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import Point

from .pkw_parser import parse_opis_granic, resolve_obwod
from .reconstruction import (
    attach_special_precincts,
    normalize_text,
    polygonal_geometry,
    reconstruct_voronoi,
)
from .sources import AdministrativeBoundaryClient, PrgClient, load_registry

try:
    import fcntl
except ImportError:  # pragma: no cover - production and CI run on Linux
    fcntl = None


WARSAW_AREA_TERYT = "146501"
WARSAW_DISTRICT_TERYTS = {f"1465{number:02d}" for number in range(2, 20)}

_PROCESS_PIPELINE: NationalReconstructionPipeline | None = None
_PROCESS_REGISTRY: pd.DataFrame | None = None


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _single_reconstruction_run(function):
    """Reject a second writer for the same snapshot instead of corrupting progress."""

    @wraps(function)
    def locked(self, *args, **kwargs):
        lock_path = self.report_dir / ".run.lock"
        # Lock files may be created by root inside Docker and later consumed by
        # an unprivileged host process. flock needs only a readable descriptor.
        descriptor = os.open(lock_path, os.O_RDONLY | os.O_CREAT, 0o666)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            if fcntl is not None:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError(
                        f"Inny przebieg rekonstrukcji już zapisuje {self.snapshot_id}"
                    ) from exc
            try:
                return function(self, *args, **kwargs)
            finally:
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    return locked


def _initialize_reconstruction_process(
    data_dir: Path, snapshot_id: str | None, registry: pd.DataFrame
) -> None:
    """Create process-local state once instead of serializing the registry per area."""
    global _PROCESS_PIPELINE, _PROCESS_REGISTRY
    _PROCESS_PIPELINE = NationalReconstructionPipeline(
        data_dir, snapshot_id=snapshot_id
    )
    _PROCESS_REGISTRY = registry


def _reconstruct_area_in_process(
    teryt: str, boundary_parts: list, force: bool
) -> dict:
    if _PROCESS_PIPELINE is None or _PROCESS_REGISTRY is None:
        raise RuntimeError("Proces rekonstrukcji nie został zainicjalizowany")
    valid_parts = [
        geometry if geometry.is_valid else shapely.make_valid(geometry)
        for geometry in boundary_parts
    ]
    boundary = shapely.union_all(valid_parts)
    _polygons, report = _PROCESS_PIPELINE.reconstruct_gmina(
        teryt, _PROCESS_REGISTRY, boundary, force=force
    )
    return report


def assign_with_pkw_parser(addresses: gpd.GeoDataFrame, registry: pd.DataFrame) -> gpd.GeoDataFrame:
    rules = [
        parse_opis_granic(int(row.precinct), str(row.area_type), str(row.description))
        for row in registry.itertuples()
    ]
    street_exact: dict[tuple[str, ...], set[int]] = {}
    street_ngrams: dict[tuple[str, ...], set[int]] = {}
    villages: dict[str, set[int]] = {}
    for index, item in enumerate(rules):
        for street_rule in item.streets:
            tokens = tuple(normalize_text(street_rule.name).split())
            if not tokens:
                continue
            street_exact.setdefault(tokens, set()).add(index)
            for start in range(len(tokens)):
                for end in range(start + 1, len(tokens) + 1):
                    street_ngrams.setdefault(tokens[start:end], set()).add(index)
        for village in item.villages:
            normalized = normalize_text(village)
            if normalized:
                villages.setdefault(normalized, set()).add(index)

    def candidates(street: str, village: str) -> list:
        indexes = set(villages.get(normalize_text(village), ()))
        tokens = tuple(normalize_text(street).split())
        if tokens:
            # Address contained in a longer rule name.
            indexes.update(street_ngrams.get(tokens, ()))
            # Rule name contained as whole consecutive words in the address.
            for start in range(len(tokens)):
                for end in range(start + 1, len(tokens) + 1):
                    indexes.update(street_exact.get(tokens[start:end], ()))
        return [rules[index] for index in sorted(indexes)]

    assigned = addresses.copy()
    results = [
        resolve_obwod(
            candidates(
                getattr(row, "street", ""), getattr(row, "miejscowosc", "")
            ),
            getattr(row, "street", ""),
            getattr(row, "number", None),
            getattr(row, "miejscowosc", ""),
        )
        for row in assigned.itertuples()
    ]
    assigned["precinct"] = pd.array([result[0] for result in results], dtype="Int64")
    assigned["match_count"] = [result[1] for result in results]
    return assigned


class NationalReconstructionPipeline:
    def __init__(
        self, data_dir: Path, prg: PrgClient | None = None, *, snapshot_id: str | None = None
    ):
        self.data_dir = data_dir
        self.prg = prg or PrgClient()
        self.snapshot_id = str(snapshot_id) if snapshot_id is not None else None
        self.address_cache = data_dir / "raw" / "prg"
        processed_root = data_dir / "processed"
        self.report_dir = data_dir / "artifacts" / "reconstruction"
        if self.snapshot_id:
            processed_root = processed_root / "snapshots" / self.snapshot_id
            self.report_dir = self.report_dir / self.snapshot_id
        self.processed_root = processed_root
        self.output_dir = processed_root / "precincts"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _area_teryts(registry: pd.DataFrame) -> list[str]:
        teryts = {str(value) for value in registry.teryt if str(value)}
        has_warsaw = bool(teryts & WARSAW_DISTRICT_TERYTS)
        teryts -= WARSAW_DISTRICT_TERYTS
        if has_warsaw:
            teryts.add(WARSAW_AREA_TERYT)
        return sorted(teryts)

    @staticmethod
    def _registry_for_area(registry: pd.DataFrame, teryt: str) -> pd.DataFrame:
        if teryt == WARSAW_AREA_TERYT:
            return registry[registry.teryt.astype(str).isin(WARSAW_DISTRICT_TERYTS)].copy()
        return registry[registry.teryt.astype(str) == teryt].copy()

    def reconstruct_gmina(
        self, teryt: str, registry: pd.DataFrame, boundary, *, force: bool = False
    ) -> tuple[gpd.GeoDataFrame, dict]:
        output = self.output_dir / f"{teryt}.parquet"
        report_path = self.report_dir / f"{teryt}.json"
        if output.exists() and report_path.exists() and not force:
            return gpd.read_parquet(output), json.loads(report_path.read_text(encoding="utf-8"))
        area_registry = self._registry_for_area(registry, teryt)
        subset = area_registry[~area_registry.special].copy()
        if subset.empty:
            raise ValueError(f"Brak terytorialnych obwodów dla {teryt}")
        addresses = self.prg.fetch(teryt, self.address_cache)
        assigned = assign_with_pkw_parser(addresses, subset)
        fallback_points = self._fallback_points(subset, assigned, boundary)
        metric_assigned = assigned.to_crs(2180)
        metric_boundary = gpd.GeoSeries([boundary], crs=4326).to_crs(2180).iloc[0]
        polygons, report = reconstruct_voronoi(
            metric_assigned, metric_boundary,
            expected_precincts=sorted(subset.precinct.unique()),
            fallback_points={key: gpd.GeoSeries([point], crs=4326).to_crs(2180).iloc[0] for key, point in fallback_points.items()},
        )
        polygons["teryt"] = teryt
        if self.snapshot_id:
            polygons["snapshot_id"] = self.snapshot_id
        attributes = subset[
            ["precinct", "teryt", "description", "commission", "eligible", "population"]
        ].drop_duplicates("precinct")
        polygons = polygons.drop(columns="teryt").merge(attributes, on="precinct", how="left")
        polygons["key"] = polygons.apply(
            lambda row: f"{row.teryt}_{row.precinct}", axis=1
        )
        fallback_precincts = set(report["fallback_precincts"])
        polygons["geometry_quality"] = polygons.precinct.map(
            lambda number: "fallback" if number in fallback_precincts else "generated"
        )
        polygons = polygons.to_crs(4326)
        polygons["geometry"] = [
            polygonal_geometry(geometry) for geometry in polygons.geometry
        ]
        attachments = self._attach_special(area_registry, addresses, polygons)
        _atomic_write_text(
            self.report_dir / f"{teryt}_special.json",
            attachments.to_json(orient="records", indent=2, force_ascii=False),
        )
        temporary_output = output.with_name(f"{output.stem}.tmp{output.suffix}")
        polygons.to_parquet(temporary_output, index=False)
        temporary_output.replace(output)
        report.update({
            "teryt": teryt,
            "snapshot_id": self.snapshot_id,
            "assignment_rate": float(assigned.precinct.notna().mean()),
            "conflicts": int(((assigned.match_count > 1) & assigned.precinct.isna()).sum()),
            "special_precincts": int(area_registry[area_registry.special].shape[0]),
            "special_attached": int(len(attachments)),
        })
        _atomic_write_text(
            report_path, json.dumps(report, ensure_ascii=False, indent=2)
        )
        return polygons, report

    @staticmethod
    def _fallback_points(subset: pd.DataFrame, assigned: gpd.GeoDataFrame, boundary) -> dict[int, Point]:
        result: dict[int, Point] = {}
        missing: list[int] = []
        for precinct in sorted({int(value) for value in subset.precinct}):
            points = assigned[assigned.precinct == precinct]
            if not points.empty:
                result[precinct] = points.geometry.union_all().centroid
            else:
                missing.append(precinct)
        if not missing:
            return result

        min_x, min_y, max_x, max_y = boundary.bounds
        side = max(8, math.ceil(math.sqrt(len(missing) * 25)))
        candidates = [boundary.representative_point()]
        for row in range(side):
            y = min_y + (row + 0.5) * (max_y - min_y) / side
            for column in range(side):
                x = min_x + (column + 0.5) * (max_x - min_x) / side
                point = Point(x, y)
                if boundary.covers(point):
                    candidates.append(point)

        occupied = list(result.values())
        for precinct in missing:
            if not candidates:
                raise ValueError(
                    f"Brak rozłącznych punktów fallback dla obwodu {precinct}"
                )
            if occupied:
                point = max(
                    candidates,
                    key=lambda candidate: (
                        min(candidate.distance(other) for other in occupied),
                        -candidate.x,
                        -candidate.y,
                    ),
                )
            else:
                point = candidates[0]
            result[precinct] = point
            occupied.append(point)
            candidates.remove(point)
        return result

    @staticmethod
    def _attach_special(registry: pd.DataFrame, addresses: gpd.GeoDataFrame, polygons: gpd.GeoDataFrame) -> pd.DataFrame:
        special = registry[registry.special].copy()
        if special.empty:
            return pd.DataFrame(columns=["special_key", "host_key", "method"])
        points = []
        for row in special.itertuples():
            street = normalize_text(getattr(row, "Ulica", ""))
            locality = normalize_text(getattr(row, "Miejscowość", ""))
            raw_number = str(getattr(row, "Numer posesji", "") or "")
            number_match = re.match(r"\s*(\d+)", raw_number)
            candidates = addresses.copy()
            if street:
                candidates = candidates[candidates.street.map(normalize_text) == street]
            if locality and "miejscowosc" in candidates:
                candidates = candidates[candidates.miejscowosc.map(normalize_text) == locality]
            if number_match and "number" in candidates:
                candidates = candidates[candidates.number == int(number_match.group(1))]
            point = candidates.geometry.iloc[0] if not candidates.empty else polygons.geometry.union_all().representative_point()
            points.append({"key": f"{row.teryt}_{row.precinct}", "geometry": point})
        commission_points = gpd.GeoDataFrame(points, crs=addresses.crs)
        special_keys = pd.DataFrame({"key": [item["key"] for item in points]})
        return attach_special_precincts(special_keys, polygons[["key", "geometry"]], commission_points)

    @_single_reconstruction_run
    def run(
        self,
        registry_path: Path,
        boundaries: gpd.GeoDataFrame | None = None,
        *,
        limit: int | None = None,
        teryts: Iterable[str] | None = None,
        retry_failed: bool = False,
        force: bool = False,
        workers: int = 0,
        progress_callback: Callable[[int, int, dict | None], None] | None = None,
    ) -> list[dict]:
        if workers < 0:
            raise ValueError("Liczba workerów nie może być ujemna")
        worker_count = workers or os.cpu_count() or 1
        registry = load_registry(registry_path)
        if boundaries is None:
            boundaries = AdministrativeBoundaryClient().fetch_gminy(self.data_dir / "raw" / "prg_boundaries").to_crs(4326)
        reports = []
        registry_teryts = self._area_teryts(registry)
        boundary_teryts = set(boundaries.teryt.astype(str))
        requested_teryts = None if teryts is None else {str(value) for value in teryts}
        selected_teryts = registry_teryts
        previous: list[dict] = []
        previous_path = self.report_dir / "national.json"
        if previous_path.is_file():
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            if not isinstance(previous, list):
                raise ValueError("Poprzedni raport national.json nie jest listą")
        if retry_failed:
            if not previous_path.is_file():
                raise ValueError("Brak poprzedniego raportu national.json do ponowienia")
            selected_teryts = [
                str(item["teryt"]) for item in previous if "error" in item
            ]
        if requested_teryts is not None:
            unknown = requested_teryts - set(registry_teryts)
            if unknown:
                raise ValueError(f"Nieznane kody TERYT: {sorted(unknown)}")
            selected_teryts = [
                value for value in selected_teryts if value in requested_teryts
            ]
        if limit:
            selected_teryts = selected_teryts[:limit]
        summary = self.report_dir / "national.json"
        cumulative = {str(item["teryt"]): item for item in previous}
        boundary_parts: dict[str, list] = {}
        for row in boundaries[["teryt", "geometry"]].itertuples(index=False):
            boundary_parts.setdefault(str(row.teryt), []).append(row.geometry)

        def process_area(teryt: str) -> dict:
            if teryt not in boundary_teryts:
                return {
                    "teryt": teryt,
                    "error": "MissingBoundary: brak granicy administracyjnej dla TERYT",
                }
            try:
                valid_parts = [
                    geometry if geometry.is_valid else shapely.make_valid(geometry)
                    for geometry in boundary_parts[teryt]
                ]
                boundary = shapely.union_all(valid_parts)
                _polygons, report = self.reconstruct_gmina(
                    teryt, registry, boundary, force=force
                )
                return report
            except Exception as exc:
                return {"teryt": teryt, "error": f"{type(exc).__name__}: {exc}"}

        # Existing results are cheap to read in the parent. Each process owns a
        # separate PrgClient/Session, so both uncached downloads and CPU work are
        # isolated; Python parsing is no longer serialized by GIL.
        pending: list[str] = []
        for teryt in selected_teryts:
            output = self.output_dir / f"{teryt}.parquet"
            report_path = self.report_dir / f"{teryt}.json"
            if output.exists() and report_path.exists() and not force:
                result = json.loads(report_path.read_text(encoding="utf-8"))
                reports.append(result)
                cumulative[teryt] = result
            else:
                pending.append(teryt)
        if progress_callback:
            progress_callback(len(reports), len(selected_teryts), None)

        if not pending:
            pass
        elif worker_count == 1:
            results = ((teryt, process_area(teryt)) for teryt in pending)
            for teryt, result in results:
                reports.append(result)
                cumulative[teryt] = result
                if progress_callback:
                    progress_callback(len(reports), len(selected_teryts), result)
                _atomic_write_text(
                    summary,
                    json.dumps(
                        [cumulative[key] for key in sorted(cumulative)],
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
        elif "reconstruct_gmina" in self.__dict__:
            # An instance-level replacement is a test/custom hook and may not be
            # pickleable. Keep its documented parallel semantics with threads.
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(process_area, teryt): teryt for teryt in pending
                }
                for future in as_completed(futures):
                    teryt = futures[future]
                    result = future.result()
                    reports.append(result)
                    cumulative[teryt] = result
                    if progress_callback:
                        progress_callback(len(reports), len(selected_teryts), result)
                    _atomic_write_text(
                        summary,
                        json.dumps(
                            [cumulative[key] for key in sorted(cumulative)],
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
        else:
            process_count = min(worker_count, max(1, len(pending)))
            with ProcessPoolExecutor(
                max_workers=process_count,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_initialize_reconstruction_process,
                initargs=(self.data_dir, self.snapshot_id, registry),
            ) as executor:
                futures = {
                    executor.submit(
                        _reconstruct_area_in_process,
                        teryt,
                        boundary_parts.get(teryt, []),
                        force,
                    ): teryt
                    for teryt in pending
                    if teryt in boundary_teryts
                }
                for teryt in pending:
                    if teryt not in boundary_teryts:
                        result = {
                            "teryt": teryt,
                            "error": "MissingBoundary: brak granicy administracyjnej dla TERYT",
                        }
                        reports.append(result)
                        cumulative[teryt] = result
                        if progress_callback:
                            progress_callback(
                                len(reports), len(selected_teryts), result
                            )
                for future in as_completed(futures):
                    teryt = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {
                            "teryt": teryt,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    reports.append(result)
                    cumulative[teryt] = result
                    if progress_callback:
                        progress_callback(len(reports), len(selected_teryts), result)
                    _atomic_write_text(
                        summary,
                        json.dumps(
                            [cumulative[key] for key in sorted(cumulative)],
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
        _atomic_write_text(
            summary,
            json.dumps(
                [cumulative[key] for key in sorted(cumulative)],
                ensure_ascii=False,
                indent=2,
            ),
        )
        reports.sort(key=lambda item: str(item["teryt"]))
        cached = sorted(self.output_dir.glob("*.parquet"))
        repaired_geometries = 0
        if cached:
            cached_frames = []
            for path in cached:
                frame = gpd.read_parquet(path)
                needs_repair = (~frame.geometry.is_valid) | ~frame.geometry.geom_type.isin(
                    ["Polygon", "MultiPolygon"]
                )
                if needs_repair.any():
                    repaired_geometries += int(needs_repair.sum())
                    frame.loc[needs_repair, "geometry"] = [
                        polygonal_geometry(geometry)
                        for geometry in frame.loc[needs_repair, "geometry"]
                    ]
                    if (~frame.geometry.is_valid).any() or not frame.geometry.geom_type.isin(
                        ["Polygon", "MultiPolygon"]
                    ).all():
                        raise ValueError(
                            f"Nie udało się naprawić geometrii cache {path.name}"
                        )
                    temporary_cache = path.with_name(
                        f"{path.stem}.tmp{path.suffix}"
                    )
                    frame.to_parquet(temporary_cache, index=False)
                    temporary_cache.replace(path)
                cached_frames.append(frame)
            national = gpd.GeoDataFrame(
                pd.concat(cached_frames, ignore_index=True),
                geometry="geometry",
                crs=4326,
            )
            parquet = self.processed_root / "precincts.parquet"
            temporary_parquet = parquet.with_name("precincts.tmp.parquet")
            national.to_parquet(temporary_parquet, index=False)
            temporary_parquet.replace(parquet)
            gpkg = self.processed_root / "precincts.gpkg"
            temporary_gpkg = gpkg.with_name("precincts.tmp.gpkg")
            if temporary_gpkg.exists():
                temporary_gpkg.unlink()
            national.to_file(temporary_gpkg, layer="precincts", driver="GPKG")
            temporary_gpkg.replace(gpkg)
        run_manifest = {
            "snapshot_id": self.snapshot_id,
            "registry_teryts": len(registry_teryts),
            "excluded_nonterritorial_precincts": int((registry.teryt.astype(str) == "").sum()),
            "selected_teryts": len(selected_teryts),
            "workers": worker_count,
            "processed_this_run": len(reports),
            "successful_this_run": sum("error" not in report for report in reports),
            "failed_this_run": sum("error" in report for report in reports),
            "successful": sum(
                "error" not in report for report in cumulative.values()
            ),
            "failed": sum("error" in report for report in cumulative.values()),
            "cached_municipalities": len(cached),
            "repaired_geometries": repaired_geometries,
            "complete_country": (
                len(cumulative) == len(registry_teryts)
                and not any("error" in report for report in cumulative.values())
                and len(cached) == len(registry_teryts)
            ),
        }
        _atomic_write_text(
            self.report_dir / "run_manifest.json",
            json.dumps(run_manifest, ensure_ascii=False, indent=2),
        )
        return reports
