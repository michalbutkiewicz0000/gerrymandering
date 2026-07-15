from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date
from fractions import Fraction
from importlib.resources import files

import networkx as nx
import yaml

from .domain import (
    ComplianceStatus,
    DistrictPlan,
    OptimizationRequest,
    ValidationFinding,
    ValidationReport,
)
from .graph import as_networkx, cut_border


_PROFILE_TEXT = files("gerry").joinpath("resources/legal_profiles.yaml").read_text(encoding="utf-8")
_PROFILE_DOCUMENT = yaml.safe_load(_PROFILE_TEXT)
LAW_DATE = date.fromisoformat(str(_PROFILE_DOCUMENT["law_snapshot"]))
LAW_PROFILE_SHA256 = hashlib.sha256(_PROFILE_TEXT.encode("utf-8")).hexdigest()
LAW_SOURCES = list(_PROFILE_DOCUMENT.get("official_sources", []))
LAW_DOCUMENTS = list(_PROFILE_DOCUMENT.get("official_documents", []))
PROFILE_RULES = _PROFILE_DOCUMENT["profiles"]
for _profile in PROFILE_RULES.values():
    _profile["target_kinds"] = set(_profile["target_kinds"])
PROFILE_CITATIONS = {
    profile_id: profile["citation"] for profile_id, profile in PROFILE_RULES.items()
}


def profile_unit_plan(profile_id: str) -> dict:
    """Granularity a profile draws districts over.

    ``unit_level`` is the atomic node (``powiat``/``gmina``/``precinct``);
    ``scope_level`` is the administrative unit the user picks to limit generation
    (``None`` means the whole country); ``split_population_gt``, when set, keeps a
    gmina un-aggregated once its population exceeds the threshold — the Senate's
    provision for splitting a city above 500 000 residents.
    """
    profile = PROFILE_RULES.get(profile_id)
    if profile is None:
        raise KeyError(profile_id)
    return {
        "unit_level": profile.get("unit_level", "precinct"),
        "scope_level": profile.get("scope_level"),
        "container_level": profile.get("container_level"),
        "split_population_gt": profile.get("split_population_gt"),
    }


class LegalValidator:
    def validate(self, request: OptimizationRequest, plan: DistrictPlan) -> ValidationReport:
        findings: list[ValidationFinding] = []
        citation = PROFILE_CITATIONS.get(request.profile_id)
        if citation is None:
            findings.append(ValidationFinding(
                code="PROFILE_UNKNOWN", status=ComplianceStatus.unverifiable,
                message=f"Nieznany profil {request.profile_id}",
            ))

        profile = PROFILE_RULES.get(request.profile_id)
        if profile:
            findings.extend(self._validate_profile_contract(request, profile, citation))

        assigned = set(plan.assignment)
        expected = set(request.nodes)
        findings.append(ValidationFinding(
            code="COVERAGE",
            status=ComplianceStatus.compliant if assigned == expected else ComplianceStatus.non_compliant,
            message="Każda jednostka musi należeć do dokładnie jednego okręgu.",
            citation=citation,
            measured={"expected": len(expected), "assigned": len(assigned)},
        ))

        if request.parent_by_node:
            parent_districts: defaultdict[str, set[int]] = defaultdict(set)
            for node, district in plan.assignment.items():
                parent_districts[request.parent_by_node.get(node, node)].add(district)
            entitlements = self.parent_entitlements(request)
            split = []
            allowed_split = []
            for parent, values in parent_districts.items():
                if len(values) <= 1:
                    continue
                permitted = False
                if profile and profile.get("parent_split_entitlement_gt") is not None:
                    permitted = entitlements.get(parent, Fraction(0)) > Fraction(
                        str(profile["parent_split_entitlement_gt"])
                    )
                if profile and profile.get("parent_split_population_gt") is not None:
                    permitted = self.parent_populations(request).get(parent, 0) > profile["parent_split_population_gt"]
                (allowed_split if permitted else split).append(parent)
            findings.append(ValidationFinding(
                code="INDIVISIBLE_PARENTS",
                status=ComplianceStatus.compliant if not split else ComplianceStatus.non_compliant,
                message="Jednostkę nadrzędną wolno dzielić wyłącznie po spełnieniu ustawowego wyjątku.",
                citation=citation, measured={"illegal_split": sorted(split), "allowed_split": sorted(allowed_split)},
            ))
            if profile and profile.get("parent_merge_entitlement_lt") is not None:
                district_parents: defaultdict[int, set[str]] = defaultdict(set)
                for node, district in plan.assignment.items():
                    district_parents[district].add(request.parent_by_node.get(node, node))
                threshold = Fraction(str(profile["parent_merge_entitlement_lt"]))
                illegal_merge = sorted(
                    district for district, parents in district_parents.items()
                    if len(parents) > 1 and all(entitlements.get(parent, 0) >= threshold for parent in parents)
                )
                findings.append(ValidationFinding(
                    code="CONDITIONAL_PARENT_MERGE",
                    status=ComplianceStatus.compliant if not illegal_merge else ComplianceStatus.non_compliant,
                    message="Łączenie jednostek nadrzędnych wymaga zbyt małej normy mandatowej co najmniej jednej z nich.",
                    citation=citation,
                    measured={
                        "illegal_districts": illegal_merge, "threshold": float(threshold),
                        "entitlements": {key: float(value) for key, value in entitlements.items()},
                    },
                ))

        if request.container_by_node:
            district_containers: defaultdict[int, set[str]] = defaultdict(set)
            for node, district in plan.assignment.items():
                district_containers[district].add(request.container_by_node[node])
            crossed = sorted(district for district, values in district_containers.items() if len(values) > 1)
            findings.append(ValidationFinding(
                code="LEGAL_CONTAINER",
                status=ComplianceStatus.compliant if not crossed else ComplianceStatus.non_compliant,
                message="Okręg przekracza granicę dozwolonego obszaru nadrzędnego.",
                citation=citation, measured={"districts": crossed},
            ))

        districts = set(plan.assignment.values())
        findings.append(ValidationFinding(
            code="DISTRICT_COUNT",
            status=ComplianceStatus.compliant if len(districts) == request.rules.district_count else ComplianceStatus.non_compliant,
            message="Liczba okręgów musi odpowiadać konfiguracji profilu.", citation=citation,
            measured={"expected": request.rules.district_count, "actual": len(districts)},
        ))

        graph = as_networkx(request.nodes, request.edges, request.rules.allowed_edge_kinds)
        disconnected = []
        for district in districts:
            members = [node for node, value in plan.assignment.items() if value == district]
            if members and not nx.is_connected(graph.subgraph(members)):
                disconnected.append(district)
        findings.append(ValidationFinding(
            code="CONTIGUITY",
            status=ComplianceStatus.compliant if not disconnected else ComplianceStatus.non_compliant,
            message="Każdy okręg musi być spójny w dozwolonym grafie sąsiedztwa.", citation=citation,
            measured={"disconnected": disconnected},
        ))

        populations = request.scenario.population_by_unit
        if set(request.nodes) <= set(populations):
            total = sum(populations[node] for node in request.nodes)
            ideal = total / request.rules.district_count
            deviations = {}
            for district in districts:
                population = sum(populations[node] for node, value in plan.assignment.items() if value == district)
                deviations[district] = abs(population - ideal) / ideal if ideal else 0
            tolerance = Fraction(str(request.rules.population_tolerance))
            compliant = all(
                abs(
                    sum(populations[node] for node, value in plan.assignment.items() if value == district)
                    * request.rules.district_count
                    - total
                )
                <= tolerance * total
                for district in districts
            )
            findings.append(ValidationFinding(
                code="CONFIGURED_POPULATION_BALANCE",
                status=ComplianceStatus.compliant if compliant else ComplianceStatus.non_compliant,
                message=(
                    "Odchylenie ludności mieści się w skonfigurowanym limicie analitycznym. "
                    "Ta kontrola nie zastępuje ustawowego algorytmu normy przedstawicielstwa."
                ),
                measured={"ideal": ideal, "deviations": deviations},
            ))
            if profile and profile.get("population_ratio_min") is not None:
                ratios = {
                    district: Fraction(
                        sum(populations[node] for node, value in plan.assignment.items() if value == district)
                        * request.rules.district_count,
                        total,
                    ) if total else Fraction(0)
                    for district in districts
                }
                minimum = Fraction(str(profile["population_ratio_min"]))
                maximum = Fraction(str(profile["population_ratio_max_exclusive"]))
                invalid = sorted(
                    district for district, ratio in ratios.items()
                    if ratio < minimum or ratio >= maximum
                )
                findings.append(ValidationFinding(
                    code="STATUTORY_POPULATION_RATIO",
                    status=ComplianceStatus.compliant if not invalid else ComplianceStatus.non_compliant,
                    message="Norma ludności okręgu musi mieścić się w ustawowym przedziale.",
                    citation=citation,
                    measured={"invalid": invalid, "ratios": {key: float(value) for key, value in ratios.items()}},
                ))
        else:
            findings.append(ValidationFinding(
                code="CONFIGURED_POPULATION_BALANCE", status=ComplianceStatus.unverifiable,
                message="Brak danych ludności dla kontroli skonfigurowanego wyrównania.",
            ))

        if request.rules.max_cut_border_m is not None:
            actual = cut_border(plan.assignment, request.edges)
            findings.append(ValidationFinding(
                code="CUT_BORDER",
                status=ComplianceStatus.compliant if actual <= request.rules.max_cut_border_m else ComplianceStatus.non_compliant,
                message="Łączna długość przeciętych granic przekracza limit konfiguracji.",
                measured={"actual": actual, "limit": request.rules.max_cut_border_m},
            ))

        structural = self._aggregate(finding.status for finding in findings)
        matches_current = bool(
            request.base_assignment
            and self._same_partition(plan.assignment, request.base_assignment)
        )
        formal = structural
        if request.profile_id.startswith("pl-") and structural != ComplianceStatus.non_compliant:
            formal = ComplianceStatus.compliant if matches_current else ComplianceStatus.requires_enactment
        findings.append(ValidationFinding(
            code="CURRENT_ENACTED_BOUNDARIES",
            status=formal,
            message=(
                "Podział odpowiada przekazanej mapie obowiązującej."
                if matches_current
                else "Nowy podział wymaga ustanowienia właściwym aktem; obowiązujące granice nie są ograniczeniem optymalizacji."
            ),
            citation=citation,
        ))
        return ValidationReport(
            structural=structural,
            formal_current=formal,
            findings=findings,
            law_snapshot=LAW_DATE,
            law_profile_sha256=LAW_PROFILE_SHA256,
            law_sources=LAW_SOURCES,
        )

    @staticmethod
    def _validate_profile_contract(request: OptimizationRequest, profile: dict, citation: str) -> list[ValidationFinding]:
        findings = []
        allowed_target = request.target_kind in profile["target_kinds"]
        findings.append(ValidationFinding(
            code="TARGET_KIND", status=ComplianceStatus.compliant if allowed_target else ComplianceStatus.non_compliant,
            message="Rodzaj celu musi być dopuszczalny dla ordynacji.", citation=citation,
            measured={"target_kind": request.target_kind, "allowed": sorted(profile["target_kinds"])},
        ))
        seat_values = (
            list(request.rules.seats_per_district.values())
            if isinstance(request.rules.seats_per_district, dict)
            else [request.rules.seats_per_district]
        )
        minimum, maximum = profile.get("min_seats"), profile.get("max_seats")
        valid_seats = all(
            (minimum is None or value >= minimum) and (maximum is None or value <= maximum)
            for value in seat_values
        )
        findings.append(ValidationFinding(
            code="SEATS_PER_DISTRICT",
            status=ComplianceStatus.compliant if valid_seats else ComplianceStatus.non_compliant,
            message="Liczba mandatów w okręgu musi mieścić się w zakresie ordynacji.", citation=citation,
            measured={"actual": seat_values, "minimum": minimum, "maximum": maximum},
        ))
        expected_count = profile.get("district_count")
        if expected_count is not None:
            findings.append(ValidationFinding(
                code="STATUTORY_DISTRICT_TOTAL",
                status=ComplianceStatus.compliant if request.rules.district_count == expected_count else ComplianceStatus.non_compliant,
                message="Łączna liczba okręgów wynika z materialnej reguły profilu.", citation=citation,
                measured={"actual": request.rules.district_count, "expected": expected_count},
            ))
        expected_total_seats = profile.get("total_seats")
        if expected_total_seats is not None:
            actual_total_seats = sum(seat_values) if isinstance(request.rules.seats_per_district, dict) else (
                request.rules.seats_per_district * request.rules.district_count
            )
            findings.append(ValidationFinding(
                code="STATUTORY_SEAT_TOTAL",
                status=ComplianceStatus.compliant if actual_total_seats == expected_total_seats else ComplianceStatus.non_compliant,
                message="Łączna liczba mandatów musi odpowiadać regule ustawowej.", citation=citation,
                measured={"actual": actual_total_seats, "expected": expected_total_seats},
            ))
        if profile.get("requires_parent"):
            findings.append(ValidationFinding(
                code="PARENT_LAYER_AVAILABLE",
                status=ComplianceStatus.compliant if request.parent_by_node else ComplianceStatus.unverifiable,
                message=f"Do pełnej weryfikacji potrzebna jest warstwa: {profile['requires_parent']}.", citation=citation,
            ))
        if profile.get("requires_container"):
            findings.append(ValidationFinding(
                code="CONTAINER_LAYER_AVAILABLE",
                status=ComplianceStatus.compliant if request.container_by_node else ComplianceStatus.unverifiable,
                message=f"Do pełnej weryfikacji potrzebny jest kontener: {profile['requires_container']}.", citation=citation,
            ))
        return findings

    @staticmethod
    def _aggregate(statuses) -> ComplianceStatus:
        statuses = list(statuses)
        if ComplianceStatus.non_compliant in statuses:
            return ComplianceStatus.non_compliant
        if ComplianceStatus.unverifiable in statuses:
            return ComplianceStatus.unverifiable
        return ComplianceStatus.compliant

    @staticmethod
    def _same_partition(left: dict[str, int], right: dict[str, int]) -> bool:
        """Compare boundaries independently of arbitrary district numbering."""
        if set(left) != set(right):
            return False
        nodes = sorted(left)
        return all(
            (left[a] == left[b]) == (right[a] == right[b])
            for index, a in enumerate(nodes)
            for b in nodes[index + 1:]
        )

    @staticmethod
    def parent_populations(request: OptimizationRequest) -> dict[str, int]:
        populations: defaultdict[str, int] = defaultdict(int)
        for node in request.nodes:
            populations[request.parent_by_node.get(node, node)] += request.scenario.population_by_unit.get(node, 0)
        return dict(populations)

    @classmethod
    def parent_entitlements(cls, request: OptimizationRequest) -> dict[str, Fraction]:
        parent_populations = cls.parent_populations(request)
        total_population = sum(request.scenario.population_by_unit.get(node, 0) for node in request.nodes)
        total_seats = (
            sum(request.rules.seats_per_district.values())
            if isinstance(request.rules.seats_per_district, dict)
            else request.rules.seats_per_district * request.rules.district_count
        )
        if not total_population or not total_seats:
            return {parent: Fraction(0) for parent in parent_populations}
        return {
            parent: Fraction(population * total_seats, total_population)
            for parent, population in parent_populations.items()
        }
