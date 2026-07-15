from __future__ import annotations

import hashlib
import itertools
import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import networkx as nx

from .domain import DistrictPlan, JobStatus, OptimizationRequest, OptimizationRun
from .elections import apply_thresholds, dhondt, plurality
from .graph import as_networkx, cut_border
from .law import LegalValidator


def canonical_assignment(assignment: dict[str, int]) -> tuple[int, ...]:
    """Remove district-label symmetry in stable node order."""
    mapping: dict[int, int] = {}
    result: list[int] = []
    for node in sorted(assignment):
        label = assignment[node]
        if label not in mapping:
            mapping[label] = len(mapping)
        result.append(mapping[label])
    return tuple(result)


def assignment_key(
    request: OptimizationRequest, assignment: dict[str, int]
) -> tuple[int, ...]:
    """Remove label symmetry only when district labels are truly interchangeable."""
    labels_are_semantic = request.base_assignment is not None or isinstance(
        request.rules.seats_per_district, dict
    )
    if labels_are_semantic:
        return tuple(assignment[node] for node in sorted(assignment))
    return canonical_assignment(assignment)


class ExactEnumerator:
    """Proof-producing reference solver for small and medium graph fixtures.

    It exhaustively enumerates canonical assignments. Production-scale requests
    are delegated to SCIP, but this solver is the executable oracle used to test
    the MIP formulation and remains useful for small municipalities.
    """

    def __init__(self, certificate_dir: Path):
        self.certificate_dir = certificate_dir
        self.certificate_dir.mkdir(parents=True, exist_ok=True)
        self.validator = LegalValidator()

    def solve(
        self, request: OptimizationRequest, cancel_requested: Callable[[], bool] | None = None
    ) -> OptimizationRun:
        run = OptimizationRun(request=request, status=JobStatus.running)
        if request.rules.district_count > len(request.nodes):
            run.status = JobStatus.infeasible
            run.message = "Więcej okręgów niż jednostek."
            return run

        graph = as_networkx(request.nodes, request.edges, request.rules.allowed_edge_kinds)
        if request.nodes and not nx.is_connected(graph):
            run.status = JobStatus.infeasible
            run.message = "Graf wejściowy jest niespójny."
            return run

        seen: set[tuple[int, ...]] = set()
        feasible: list[tuple[tuple, DistrictPlan]] = []
        evaluated = 0
        for raw_index, labels in enumerate(
            itertools.product(range(request.rules.district_count), repeat=len(request.nodes))
        ):
            if raw_index % 1000 == 0 and cancel_requested and cancel_requested():
                run.status = JobStatus.cancelled
                run.message = f"Anulowano po sprawdzeniu {evaluated} kanonicznych podziałów."
                return run
            if set(labels) != set(range(request.rules.district_count)):
                continue
            assignment = dict(zip(request.nodes, labels))
            canonical = assignment_key(request, assignment)
            if canonical in seen:
                continue
            seen.add(canonical)
            evaluated += 1
            plan = self._evaluate(request, assignment)
            report = self.validator.validate(request, plan)
            plan.validation = report
            if report.structural.value == "NON_COMPLIANT":
                continue
            objective = self._objective(request, plan, graph)
            feasible.append((objective, plan))

        if not feasible:
            run.status = JobStatus.infeasible
            run.message = f"Udowodniono brak rozwiązania po sprawdzeniu {evaluated} podziałów."
            run.certificate_path = str(self._write_certificate(run, evaluated, []))
            run.certificate_verified = self.verify_certificate(
                Path(run.certificate_path), cancel_requested=cancel_requested
            )
            if cancel_requested and cancel_requested():
                run.status = JobStatus.cancelled
                run.message = "Anulowano podczas niezależnej weryfikacji certyfikatu."
            return run

        feasible.sort(key=lambda item: item[0], reverse=True)
        best_objective = feasible[0][0]
        primary = best_objective[0]
        best = feasible[0][1]
        alternatives = []
        best_cut_set = self._cut_set(best.assignment, request)
        for _, candidate in feasible[1:]:
            if candidate.target_seats != primary:
                continue
            candidate_cut_set = self._cut_set(candidate.assignment, request)
            universe = best_cut_set | candidate_cut_set
            diversity = len(best_cut_set ^ candidate_cut_set) / max(1, len(universe))
            if diversity >= 0.05:
                alternatives.append(candidate)
            if len(alternatives) >= request.alternatives - 1:
                break

        run.incumbent = best
        run.alternatives = alternatives
        run.best_bound = best.target_seats
        run.status = JobStatus.optimal
        run.message = f"Udowodniono optimum po sprawdzeniu {evaluated} kanonicznych podziałów."
        run.certificate_path = str(self._write_certificate(run, evaluated, [best_objective]))
        run.certificate_verified = self.verify_certificate(
            Path(run.certificate_path), cancel_requested=cancel_requested
        )
        if cancel_requested and cancel_requested():
            run.status = JobStatus.cancelled
            run.message = "Anulowano podczas niezależnej weryfikacji certyfikatu."
            return run
        if not run.certificate_verified:
            run.status = JobStatus.failed
            run.message = "Wewnętrzna weryfikacja certyfikatu nie powiodła się."
        return run

    def _evaluate(self, request: OptimizationRequest, assignment: dict[str, int]) -> DistrictPlan:
        votes: defaultdict[int, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
        populations: defaultdict[int, int] = defaultdict(int)
        candidate_district = assignment.get(request.candidate_anchor) if request.candidate_anchor else None
        for node, district in assignment.items():
            for party, value in request.scenario.votes_by_unit.get(node, {}).items():
                if request.target_kind == "candidate" and party == request.target and district != candidate_district:
                    continue
                votes[district][party] += value
            populations[district] += request.scenario.population_by_unit.get(node, 0)

        seats_by_district: dict[int, dict[str, int]] = {}
        target_seats = 0
        proportional = request.profile_id not in {"generic-jow", "pl-senat@2026-07-15", "pl-rada-gminy-do-20k@2026-07-15"}
        national_votes: defaultdict[str, int] = defaultdict(int)
        for unit_votes in request.scenario.votes_by_unit.values():
            for party, value in unit_votes.items():
                national_votes[party] += value
        eligible_votes = apply_thresholds(
            dict(national_votes), request.scenario.thresholds, request.scenario.threshold_exempt
        )
        eligible = set(eligible_votes)
        for district in range(request.rules.district_count):
            seat_count = request.rules.seats_per_district
            if isinstance(seat_count, dict):
                seat_count = seat_count[district]
            result = (
                dhondt({party: value for party, value in votes[district].items() if party in eligible}, seat_count)
                if proportional else plurality(dict(votes[district]))
            )
            seats_by_district[district] = result.seats
            if request.target_kind != "candidate" or district == candidate_district:
                target_seats += result.seats.get(request.target, 0)

        total_population = sum(populations.values())
        ideal = total_population / request.rules.district_count if request.rules.district_count else 0
        deviation = sum(abs(value - ideal) for value in populations.values()) / total_population if total_population else 0
        changed = 0
        if request.base_assignment:
            changed = sum(request.base_assignment.get(node) != district for node, district in assignment.items())
        return DistrictPlan(
            assignment=assignment,
            seats_by_district=seats_by_district,
            target_seats=target_seats,
            cut_border_m=cut_border(assignment, request.edges),
            population_deviation=deviation,
            changed_units=changed,
        )

    @staticmethod
    def _objective(request: OptimizationRequest, plan: DistrictPlan, graph: nx.Graph) -> tuple:
        del graph  # connectivity is validated separately; metrics below are integer-exact
        max_dev_scaled = 0
        total_dev_scaled = 0
        populations = request.scenario.population_by_unit
        if populations:
            total = sum(populations.get(node, 0) for node in request.nodes)
            district_population: defaultdict[int, int] = defaultdict(int)
            for node, district in plan.assignment.items():
                district_population[district] += populations.get(node, 0)
            scaled = [
                abs(district_population[district] * request.rules.district_count - total)
                for district in range(request.rules.district_count)
            ]
            max_dev_scaled = max(scaled, default=0)
            total_dev_scaled = sum(scaled)
        cut_border_mm = sum(
            round(edge.shared_border_m * 1000)
            for edge in request.edges
            if plan.assignment[edge.source] != plan.assignment[edge.target]
        )
        return (
            plan.target_seats,
            -max_dev_scaled,
            -plan.changed_units,
            -cut_border_mm,
            -total_dev_scaled,
        )

    @staticmethod
    def _cut_set(assignment: dict[str, int], request: OptimizationRequest) -> set[tuple[str, str]]:
        return {
            (edge.source, edge.target)
            for edge in request.edges
            if assignment[edge.source] != assignment[edge.target]
        }

    def _write_certificate(self, run: OptimizationRun, evaluated: int, objective: list[tuple]) -> Path:
        payload = {
            "algorithm": "exhaustive-canonical-enumeration-v1",
            "run_id": str(run.id),
            "request": run.request.model_dump(mode="json", exclude_none=True),
            "request_sha256": hashlib.sha256(
                run.request.model_dump_json(exclude_none=True).encode("utf-8")
            ).hexdigest(),
            "evaluated_assignments": evaluated,
            "status": run.status.value,
            "best_bound": run.best_bound,
            "objective": objective,
        }
        payload["certificate_sha256"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        path = self.certificate_dir / f"{run.id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def verify_certificate(
        path: Path, cancel_requested: Callable[[], bool] | None = None
    ) -> bool:
        payload = json.loads(path.read_text(encoding="utf-8"))
        claimed = payload.pop("certificate_sha256", None)
        actual = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        if claimed != actual or payload.get("algorithm") != "exhaustive-canonical-enumeration-v1":
            return False
        try:
            request = OptimizationRequest.model_validate(payload["request"])
            expected_request_hash = hashlib.sha256(
                request.model_dump_json(exclude_none=True).encode("utf-8")
            ).hexdigest()
            if expected_request_hash != payload["request_sha256"]:
                return False
            verifier = ExactEnumerator(path.parent / ".verify-scratch")
            graph = as_networkx(request.nodes, request.edges, request.rules.allowed_edge_kinds)
            seen: set[tuple[int, ...]] = set()
            objectives = []
            evaluated = 0
            for raw_index, labels in enumerate(itertools.product(
                range(request.rules.district_count), repeat=len(request.nodes)
            )):
                if raw_index % 1000 == 0 and cancel_requested and cancel_requested():
                    return False
                if set(labels) != set(range(request.rules.district_count)):
                    continue
                assignment = dict(zip(request.nodes, labels))
                canonical = assignment_key(request, assignment)
                if canonical in seen:
                    continue
                seen.add(canonical)
                evaluated += 1
                plan = verifier._evaluate(request, assignment)
                if verifier.validator.validate(request, plan).structural.value == "NON_COMPLIANT":
                    continue
                objectives.append(verifier._objective(request, plan, graph))
            expected_objective = [max(objectives)] if objectives else []
            # JSON normalization handles tuples without relying on Python repr.
            normalized = json.loads(json.dumps(expected_objective))
            expected_status = JobStatus.optimal.value if objectives else JobStatus.infeasible.value
            return (
                evaluated == payload["evaluated_assignments"]
                and normalized == payload["objective"]
                and payload["status"] == expected_status
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
