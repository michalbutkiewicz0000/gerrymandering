from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import MultiPoint, Point


class Parity(StrEnum):
    any = "any"
    even = "even"
    odd = "odd"


def normalize_text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = unicodedata.normalize("NFKD", str(value).lower().translate(str.maketrans({"ł": "l", "đ": "d"})))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"\b(ulica|ul\.?|aleja|al\.?|plac|pl\.?|osiedle|os\.?)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def number_prefix(value: Any) -> int | None:
    match = re.match(r"\s*(\d+)", str(value or ""))
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class AddressRule:
    street: str = ""
    village: str = ""
    minimum: int | None = None
    maximum: int | None = None
    parity: Parity = Parity.any

    def matches(self, street: str, number: Any, village: str = "") -> bool:
        normalized_street = normalize_text(street)
        normalized_village = normalize_text(village)
        if self.street and normalize_text(self.street) != normalized_street:
            return False
        if self.village and normalize_text(self.village) != normalized_village:
            return False
        parsed = number_prefix(number)
        if self.minimum is not None and (parsed is None or parsed < self.minimum):
            return False
        if self.maximum is not None and (parsed is None or parsed > self.maximum):
            return False
        if parsed is not None and self.parity == Parity.even and parsed % 2:
            return False
        if parsed is not None and self.parity == Parity.odd and not parsed % 2:
            return False
        return True

    @property
    def specificity(self) -> int:
        return 4 * bool(self.street) + 4 * bool(self.village) + 2 * (
            self.minimum is not None or self.maximum is not None
        ) + (self.parity != Parity.any)


@dataclass
class PrecinctRules:
    precinct: int
    rules: list[AddressRule] = field(default_factory=list)
    raw: str = ""


RANGE_RE = re.compile(r"(?P<start>\d+)\s*(?:-|–|—|do)\s*(?P<end>\d+)", re.I)


def _parse_fragment(fragment: str) -> tuple[int | None, int | None, Parity]:
    normalized = normalize_text(fragment)
    parity = Parity.odd if "nieparzyst" in normalized else Parity.even if "parzyst" in normalized else Parity.any
    range_match = RANGE_RE.search(fragment)
    if range_match:
        return int(range_match.group("start")), int(range_match.group("end")), parity
    numbers = [int(value) for value in re.findall(r"\b\d+\b", fragment)]
    if len(numbers) == 1:
        return numbers[0], numbers[0], parity
    return None, None, parity


def parse_boundary_description(precinct: int, area_type: str, raw: str) -> PrecinctRules:
    """Parse the common PKW street/village grammar into auditable rules.

    Unknown clauses remain in ``raw`` and lower reconstruction confidence; the
    parser never silently invents a match.
    """
    text = str(raw or "").strip()
    result = PrecinctRules(precinct=precinct, raw=text)
    is_rural = any(token in normalize_text(area_type) for token in ("wies", "solectwo"))

    for clause in re.split(r"[;\n]+", text):
        clause = clause.strip(" ,.")
        if not clause:
            continue
        if is_rural and not re.search(r"\bul\.?\b|\bulica\b", clause, re.I):
            for village in re.split(r",|\boraz\b", clause):
                village = re.sub(r"^(miejscowosc|miejscowosci|solectwo)\s*:?", "", village, flags=re.I).strip()
                if village:
                    result.rules.append(AddressRule(village=village))
            continue

        # Handles both "ul. X 1-9" and Warsaw's "ul. X: 1-9 nieparzyste".
        street_match = re.search(
            r"(?:ul\.?|ulica|al\.?|aleja|pl\.?|plac)?\s*([^:,]+?)(?=\s*[:]|\s+\d|\s*\(|$)", clause, re.I
        )
        if street_match:
            street = street_match.group(1).strip()
            minimum, maximum, parity = _parse_fragment(clause[street_match.end():])
            result.rules.append(
                AddressRule(street=street, minimum=minimum, maximum=maximum, parity=parity)
            )
    return result


def assign_addresses(addresses: gpd.GeoDataFrame, precinct_rules: list[PrecinctRules]) -> gpd.GeoDataFrame:
    assigned = addresses.copy()
    values: list[int | None] = []
    counts: list[int] = []
    for row in assigned.itertuples():
        matches: list[tuple[int, int]] = []
        for rules in precinct_rules:
            specificity = max(
                (rule.specificity for rule in rules.rules if rule.matches(
                    getattr(row, "street", ""),
                    getattr(row, "number", getattr(row, "housenumber", "")),
                    getattr(row, "village", getattr(row, "miejscowosc", "")),
                )),
                default=-1,
            )
            if specificity >= 0:
                matches.append((rules.precinct, specificity))
        counts.append(len(matches))
        if not matches:
            values.append(None)
            continue
        best = max(score for _, score in matches)
        winners = sorted({precinct for precinct, score in matches if score == best})
        values.append(winners[0] if len(winners) == 1 else None)
    assigned["precinct"] = pd.array(values, dtype="Int64")
    assigned["match_count"] = counts
    return assigned


def reconstruct_voronoi(
    assigned: gpd.GeoDataFrame,
    boundary,
    *,
    expected_precincts: list[int],
    fallback_points: dict[int, Point] | None = None,
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    """Create a gap-free partition and guarantee a seed for every territorial precinct."""
    fallback_points = fallback_points or {}
    points = assigned.dropna(subset=["precinct"])[["precinct", "geometry"]].copy()
    points = gpd.GeoDataFrame(points, geometry="geometry", crs=assigned.crs)
    present = set(points["precinct"].astype(int))
    fallback_used: list[int] = []
    for precinct in expected_precincts:
        if precinct in present:
            continue
        point = fallback_points.get(precinct)
        if point is None:
            # Deterministic small displacement avoids coincident fallback seeds.
            center = boundary.representative_point()
            point = Point(center.x + precinct * 1e-8, center.y + precinct * 1e-8)
        points.loc[len(points)] = {"precinct": precinct, "geometry": point}
        fallback_used.append(int(precinct))

    if points.empty:
        raise ValueError("reconstruction requires at least one territorial precinct")
    points = gpd.GeoDataFrame(points, geometry="geometry", crs=assigned.crs)
    if not boundary.is_valid:
        boundary = shapely.make_valid(boundary)
    regions = shapely.voronoi_polygons(MultiPoint(points.geometry.tolist()), extend_to=boundary)
    cells = gpd.GeoDataFrame(geometry=list(regions.geoms), crs=assigned.crs)
    joined = gpd.sjoin(cells, points, predicate="contains", how="inner")
    # GEOS can report a side-location conflict for formally valid inputs whose
    # segments differ only below floating-point precision. A 1 µm precision
    # grid in projected data (or a picodegree in geographic data) makes the
    # overlay robust without changing a meaningful electoral boundary.
    projected = bool(assigned.crs and assigned.crs.is_projected)
    grid_size = 1e-6 if projected else 1e-12
    joined["geometry"] = shapely.intersection(
        shapely.make_valid(joined.geometry.array),
        shapely.make_valid(boundary),
        grid_size=grid_size,
    )
    result = joined.dissolve(by="precinct").reset_index()[["precinct", "geometry"]]
    result["precinct"] = result["precinct"].astype(int)
    union = result.geometry.union_all()
    report = {
        "addresses_total": int(len(assigned)),
        "addresses_assigned": int(assigned["precinct"].notna().sum()),
        "precincts_expected": len(expected_precincts),
        "precincts_generated": int(len(result)),
        "fallback_precincts": fallback_used,
        "coverage_ratio": min(1.0, float(union.area / boundary.area)) if boundary.area else 0.0,
        "overlap_free": abs(sum(result.geometry.area) - union.area) <= max(1e-9, boundary.area * 1e-9),
    }
    return result, report


def attach_special_precincts(
    special: pd.DataFrame, territorial: gpd.GeoDataFrame, commission_points: gpd.GeoDataFrame
) -> pd.DataFrame:
    """Attach non-territorial precincts to the polygon containing their commission."""
    if special.empty:
        return pd.DataFrame(columns=["special_key", "host_key", "method"])
    located = commission_points.merge(special[["key"]], on="key", how="inner")
    joined = gpd.sjoin(located, territorial[["key", "geometry"]], predicate="within", how="left", lsuffix="special", rsuffix="host")
    rows = []
    for row in joined.itertuples():
        host = getattr(row, "key_host", None)
        method = "contains_commission"
        if not host:
            distances = territorial.geometry.distance(row.geometry)
            host = territorial.iloc[int(distances.argmin())]["key"]
            method = "nearest_commission"
        rows.append({"special_key": getattr(row, "key_special"), "host_key": host, "method": method})
    return pd.DataFrame(rows)
