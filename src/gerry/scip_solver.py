from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from fractions import Fraction
from math import ceil, floor
from pathlib import Path

from .domain import DistrictPlan, JobStatus, OptimizationRequest, OptimizationRun
from .elections import dhondt, plurality
from .graph import cut_border
from .law import LegalValidator, PROFILE_RULES


class ScipUnavailable(RuntimeError):
    pass


def verify_vipr_manifest(
    path: Path,
    request: OptimizationRequest | None = None,
    *,
    rerun_viprchk: bool = False,
) -> tuple[bool, str]:
    """Verify persisted model/proof integrity and optionally rerun VIPR."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            payload.get("schema_version") != 2
            or payload.get("algorithm") != "scip-exact-lexicographic-vipr-v2"
        ):
            return False, "Nieobsługiwany schemat manifestu VIPR"
        proofs = payload["proofs"]
        if len(proofs) != payload["expected_stages"] or not proofs:
            return False, "Niekompletna liczba etapów dowodu"
        if request is not None:
            request_hash = hashlib.sha256(
                request.model_dump_json().encode("utf-8")
            ).hexdigest()
            if request_hash != payload.get("request_sha256"):
                return False, "Manifest nie odpowiada żądaniu optymalizacji"
        root = path.parent.resolve()
        verifier = shutil.which("viprchk") if rerun_viprchk else None
        if rerun_viprchk and verifier is None:
            return False, "Brak viprchk w PATH"
        for record in proofs:
            if not record.get("verified"):
                return False, f"Etap {record.get('stage')} nie był zweryfikowany"
            model_path = Path(record["model_path"]).resolve()
            proof_path = Path(record["checked_artifact"]).resolve()
            if not model_path.is_relative_to(root) or not proof_path.is_relative_to(root):
                return False, "Artefakt manifestu znajduje się poza katalogiem certyfikatu"
            if not model_path.is_file() or not proof_path.is_file():
                return False, "Brak modelu CIP lub dowodu VIPR"
            if hashlib.sha256(model_path.read_bytes()).hexdigest() != record["model_sha256"]:
                return False, f"Niezgodna suma modelu etapu {record.get('stage')}"
            if hashlib.sha256(proof_path.read_bytes()).hexdigest() != record["proof_sha256"]:
                return False, f"Niezgodna suma dowodu etapu {record.get('stage')}"
            if verifier:
                verification = subprocess.run(
                    [verifier, str(proof_path)],
                    capture_output=True,
                    text=True,
                    timeout=None,
                    check=False,
                )
                if verification.returncode != 0:
                    return False, f"viprchk odrzucił etap {record.get('stage')}"
        return True, "Manifest i wszystkie etapy dowodu są spójne"
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return False, f"Nieprawidłowy manifest: {type(exc).__name__}: {exc}"


def exact_scip_available() -> tuple[bool, str]:
    """Probe the actual linked SCIP library, not merely the Python package version."""
    try:
        from pyscipopt import Model
    except ImportError:
        return False, "PySCIPOpt nie jest zainstalowany"
    model = Model("exact-capability-probe")
    version = f"{model.getMajorVersion()}.{model.getMinorVersion()}.{model.getTechVersion()}"
    # PySCIPOpt creates a basic problem in Model.__init__, while SCIP requires
    # exact mode to be selected in INIT, before any problem exists.
    model.freeProb()
    try:
        model.enableExactSolving(True)
    except Exception:
        return False, f"SCIP {version} skompilowano bez EXACTSOLVE"
    return bool(model.isExact()), f"SCIP {version} EXACTSOLVE"


class ScipExactSolver:
    """Exact connected-district MIP for JOW and D'Hondt profiles.

    The exhaustive solver remains the oracle. This formulation is intentionally
    D'Hondt is encoded by selecting the globally highest quotient slots in each
    district and enforcing every selected/unselected quotient comparison with
    integer cross-products.
    """

    def __init__(self, artifact_dir: Path, *, require_exact: bool = True):
        self.artifact_dir = artifact_dir
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.require_exact = require_exact

    def solve(
        self, request: OptimizationRequest, cancel_requested: Callable[[], bool] | None = None
    ) -> OptimizationRun:
        try:
            from pyscipopt import Eventhdlr, Model, SCIP_EVENTTYPE, quicksum
        except ImportError as exc:
            raise ScipUnavailable("Zainstaluj opcjonalną zależność solver: pip install '.[solver]'") from exc
        jow_profiles = {"generic-jow", "pl-senat@2026-07-15", "pl-rada-gminy-do-20k@2026-07-15"}
        proportional_profiles = {
            "generic-proportional", "pl-sejm@2026-07-15",
            "pl-rada-gminy-powyzej-20k@2026-07-15", "pl-rada-powiatu@2026-07-15",
            "pl-sejmik@2026-07-15",
        }
        if request.profile_id not in jow_profiles | proportional_profiles:
            raise ValueError(f"Unsupported exact profile: {request.profile_id}")
        proportional = request.profile_id in proportional_profiles

        run = OptimizationRun(request=request, status=JobStatus.running)
        nodes = list(request.nodes)
        districts = range(request.rules.district_count)
        n = len(nodes)
        problem_name = f"districting-{run.id}"
        model = Model(problem_name)
        if self.require_exact:
            available, detail = exact_scip_available()
            if not available:
                raise ScipUnavailable(
                    f"{detail}. Użyj obrazu Docker projektu albo solvera "
                    "wyczerpującego dla maksymalnie 14 węzłów."
                )

        def attach_cancellation(target_model) -> None:
            if not cancel_requested:
                return

            class CancellationHandler(Eventhdlr):
                def __init__(self):
                    super().__init__()
                    self.last_check = 0.0

                def eventinit(self):
                    self.model.catchEvent(SCIP_EVENTTYPE.NODESOLVED, self)

                def eventexit(self):
                    self.model.dropEvent(SCIP_EVENTTYPE.NODESOLVED, self)

                def eventexec(self, event):
                    del event
                    now = time.monotonic()
                    if now - self.last_check >= 1:
                        if cancel_requested():
                            self.model.interruptSolve()
                        self.last_check = now

            target_model.includeEventhdlr(
                CancellationHandler(), "gerry-cancel", "Przerywa anulowane zadanie"
            )

        if not self.require_exact:
            attach_cancellation(model)
        x = {(node, d): model.addVar(vtype="B", name=f"x_{node}_{d}") for node in nodes for d in districts}
        root = {(node, d): model.addVar(vtype="B", name=f"r_{node}_{d}") for node in nodes for d in districts}
        size = {d: model.addVar(vtype="I", lb=1, ub=n, name=f"size_{d}") for d in districts}
        rooted_size = {(node, d): model.addVar(vtype="C", lb=0, ub=n, name=f"q_{node}_{d}") for node in nodes for d in districts}

        allowed = [edge for edge in request.edges if edge.kind in request.rules.allowed_edge_kinds]
        arcs = [(edge.source, edge.target) for edge in allowed] + [(edge.target, edge.source) for edge in allowed]
        flow = {(u, v, d): model.addVar(lb=0, ub=n - 1, name=f"f_{u}_{v}_{d}") for u, v in arcs for d in districts}

        for node in nodes:
            model.addCons(quicksum(x[node, d] for d in districts) == 1)
        grouped: defaultdict[str, list[str]] = defaultdict(list)
        for node, parent in request.parent_by_node.items():
            grouped[parent].append(node)
        profile_rules = PROFILE_RULES.get(request.profile_id, {})
        entitlements = LegalValidator.parent_entitlements(request)
        parent_populations = LegalValidator.parent_populations(request)
        splittable = set()
        for parent in grouped:
            if profile_rules.get("parent_split_entitlement_gt") is not None and entitlements.get(parent, 0) > Fraction(
                str(profile_rules["parent_split_entitlement_gt"])
            ):
                splittable.add(parent)
            if profile_rules.get("parent_split_population_gt") is not None and parent_populations.get(parent, 0) > profile_rules[
                "parent_split_population_gt"
            ]:
                splittable.add(parent)
        for parent, members in grouped.items():
            if parent in splittable:
                continue
            for node in members[1:]:
                for d in districts:
                    model.addCons(x[node, d] == x[members[0], d])
        merge_threshold = profile_rules.get("parent_merge_entitlement_lt")
        if grouped and merge_threshold is not None:
            parent_used = {
                (parent, d): model.addVar(vtype="B", name=f"parent_{parent}_{d}")
                for parent in grouped for d in districts
            }
            for parent, members in grouped.items():
                for d in districts:
                    for node in members:
                        model.addCons(x[node, d] <= parent_used[parent, d])
                    model.addCons(parent_used[parent, d] <= quicksum(x[node, d] for node in members))
            threshold = Fraction(str(merge_threshold))
            large = sorted(parent for parent in grouped if entitlements.get(parent, 0) >= threshold)
            for index, left in enumerate(large):
                for right in large[index + 1:]:
                    for d in districts:
                        model.addCons(parent_used[left, d] + parent_used[right, d] <= 1)
        if request.container_by_node:
            for d in districts:
                containers = sorted(set(request.container_by_node.values()))
                used = {container: model.addVar(vtype="B", name=f"container_{container}_{d}") for container in containers}
                model.addCons(quicksum(used.values()) <= 1)
                for node in nodes:
                    model.addCons(x[node, d] <= used[request.container_by_node[node]])
        for d in districts:
            model.addCons(size[d] == quicksum(x[node, d] for node in nodes))
            model.addCons(quicksum(root[node, d] for node in nodes) == 1)
            for node in nodes:
                model.addCons(root[node, d] <= x[node, d])
                q = rooted_size[node, d]
                model.addCons(q <= n * root[node, d])
                model.addCons(q <= size[d])
                model.addCons(q >= size[d] - n * (1 - root[node, d]))
                incoming = quicksum(flow[u, v, d] for u, v in arcs if v == node)
                outgoing = quicksum(flow[u, v, d] for u, v in arcs if u == node)
                model.addCons(incoming - outgoing == x[node, d] - q)
            for u, v in arcs:
                model.addCons(flow[u, v, d] <= (n - 1) * x[u, d])
                model.addCons(flow[u, v, d] <= (n - 1) * x[v, d])

        populations = request.scenario.population_by_unit
        max_population_deviation = None
        total_population_deviation = None
        if set(nodes) <= set(populations):
            total = sum(populations[node] for node in nodes)
            tolerance = Fraction(str(request.rules.population_tolerance))
            exact_ideal = Fraction(total, request.rules.district_count)
            lower = ceil(exact_ideal * (1 - tolerance))
            upper = floor(exact_ideal * (1 + tolerance))
            deviations = {}
            for d in districts:
                population = quicksum(populations[node] * x[node, d] for node in nodes)
                model.addCons(population >= lower)
                model.addCons(population <= upper)
                deviations[d] = model.addVar(vtype="I", lb=0, ub=request.rules.district_count * total, name=f"popdev_{d}")
                scaled = request.rules.district_count * population - total
                model.addCons(deviations[d] >= scaled)
                model.addCons(deviations[d] >= -scaled)
            max_population_deviation = model.addVar(
                vtype="I", lb=0, ub=request.rules.district_count * total, name="max_popdev"
            )
            for value in deviations.values():
                model.addCons(max_population_deviation >= value)
            total_population_deviation = quicksum(deviations.values())

        changed_expression = None
        if request.base_assignment:
            changed_expression = quicksum(
                1 - x[node, request.base_assignment[node]]
                for node in nodes if node in request.base_assignment
            )

        cut_variables = {}
        for index, edge in enumerate(allowed):
            cut_variables[index] = model.addVar(vtype="B", name=f"cut_{index}")
            for d in districts:
                model.addCons(cut_variables[index] >= x[edge.source, d] - x[edge.target, d])
                model.addCons(cut_variables[index] >= x[edge.target, d] - x[edge.source, d])
        cut_expression = quicksum(
            round(allowed[index].shared_border_m * 1000) * variable
            for index, variable in cut_variables.items()
        )
        if request.rules.max_cut_border_m is not None:
            model.addCons(cut_expression <= floor(request.rules.max_cut_border_m * 1000))

        parties = sorted({party for votes in request.scenario.votes_by_unit.values() for party in votes})
        # Committee names carry spaces (e.g. "KOALICYJNY KOMITET WYBORCZY …") and the
        # VIPR certificate writer rejects variable names with whitespace, so index
        # committees by their stable position in the sorted list for SCIP names.
        party_index = {party: index for index, party in enumerate(parties)}
        national_votes = {
            party: sum(votes.get(party, 0) for votes in request.scenario.votes_by_unit.values())
            for party in parties
        }
        national_total = sum(national_votes.values())
        eligible = [
            party for party in parties
            if party in request.scenario.threshold_exempt
            or not national_total
            or Fraction(national_votes[party], national_total)
            >= Fraction(str(request.scenario.thresholds.get(party, 0)))
        ]
        big_m = 1 + sum(sum(votes.values()) for votes in request.scenario.votes_by_unit.values())
        district_votes = {
            (party, d): quicksum(
                request.scenario.votes_by_unit.get(node, {}).get(party, 0) * x[node, d]
                for node in nodes
            )
            for party in parties for d in districts
        }
        wins = {}
        quotient_selected = {}
        if not proportional:
            for d in districts:
                wins[d] = model.addVar(vtype="B", name=f"win_{d}")
                if request.target_kind == "candidate":
                    model.addCons(wins[d] <= x[request.candidate_anchor, d])
                for party in parties:
                    if party == request.target:
                        continue
                    # Same deterministic tie policy as elections.plurality:
                    # alphabetically first name wins an equal vote count.
                    strict = int(request.target > party)
                    model.addCons(
                        district_votes[request.target, d] >= district_votes[party, d] + strict
                        - big_m * (1 - wins[d])
                    )
            primary_objective = quicksum(wins.values())
        else:
            max_seats = max(
                request.rules.seats_per_district.values()
                if isinstance(request.rules.seats_per_district, dict)
                else [request.rules.seats_per_district]
            )
            comparison_m = 2 * max(1, max_seats) * max(1, big_m)
            for d in districts:
                seats_d = (
                    request.rules.seats_per_district[d]
                    if isinstance(request.rules.seats_per_district, dict)
                    else request.rules.seats_per_district
                )
                slots = [(party, divisor) for party in eligible for divisor in range(1, seats_d + 1)]
                for party, divisor in slots:
                    quotient_selected[party, divisor, d] = model.addVar(
                        vtype="B", name=f"quot_p{party_index[party]}_{divisor}_{d}"
                    )
                    if divisor > 1:
                        model.addCons(
                            quotient_selected[party, divisor, d]
                            <= quotient_selected[party, divisor - 1, d]
                        )
                model.addCons(quicksum(quotient_selected[p, q, d] for p, q in slots) == seats_d)
                for party_a, divisor_a in slots:
                    for party_b, divisor_b in slots:
                        if (party_a, divisor_a) == (party_b, divisor_b):
                            continue
                        selected_a = quotient_selected[party_a, divisor_a, d]
                        selected_b = quotient_selected[party_b, divisor_b, d]
                        # Alphabetical order is a deterministic analytical tie policy.
                        strict = int((party_a, divisor_a) > (party_b, divisor_b))
                        model.addCons(
                            divisor_b * district_votes[party_a, d]
                            - divisor_a * district_votes[party_b, d]
                            >= strict - comparison_m * (1 - selected_a + selected_b)
                        )
            primary_objective = quicksum(
                value for (party, _divisor, _district), value in quotient_selected.items()
                if party == request.target
            )
        model.setParam("display/verblevel", 0)
        stages = [("target_seats", primary_objective, "maximize")]
        if max_population_deviation is not None:
            stages.append(("max_population_deviation", max_population_deviation, "minimize"))
        if changed_expression is not None:
            stages.append(("changed_units", changed_expression, "minimize"))
        stages.append(("cut_border_mm", cut_expression, "minimize"))
        if total_population_deviation is not None:
            stages.append(("total_population_deviation", total_population_deviation, "minimize"))
        stage_values = {}
        proof_entries: list[tuple[str, Path, Path]] = []
        solved_model = model
        model_paths: list[Path] = []
        exact_assignment: dict[str, int] | None = None

        def optimize_serialized(
            serialized_path: Path, proof_path: Path
        ) -> dict:
            """Solve one exact CIP in an isolated native process."""
            result_path = serialized_path.with_suffix(".result.json")
            result_path.unlink(missing_ok=True)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "gerry.exact_stage",
                    str(serialized_path),
                    str(proof_path),
                    str(result_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    if cancel_requested and cancel_requested():
                        process.terminate()
                        stdout, stderr = process.communicate()
                        return {
                            "status": "userinterrupt", "nsols": 0,
                            "proof_logging": False, "assignment_values": {},
                        }
            if process.returncode != 0 or not result_path.exists():
                detail = (stdout + stderr)[-4000:]
                raise RuntimeError(
                    f"Izolowany etap SCIP zakończył się kodem {process.returncode}: {detail}"
                )
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            if "error" in payload:
                raise RuntimeError(payload["error"])
            return payload

        def assignment_from_exact_result(result: dict) -> dict[str, int]:
            values = result["assignment_values"]
            return {
                node: next(
                    d for d in districts if values[f"x_{node}_{d}"] > 0.5
                )
                for node in nodes
            }

        def exact_integer_objectives(assignment: dict[str, int]) -> dict[str, int]:
            """Recompute integral objectives without converting SCIP rationals to float."""
            plan = self._build_plan(request, assignment, set(eligible), proportional)
            values = {
                "target_seats": plan.target_seats,
                "changed_units": plan.changed_units,
                "cut_border_mm": sum(
                    round(edge.shared_border_m * 1000)
                    for edge in allowed
                    if assignment[edge.source] != assignment[edge.target]
                ),
            }
            if set(nodes) <= set(populations):
                district_populations = {
                    d: sum(
                        populations[node]
                        for node in nodes
                        if assignment[node] == d
                    )
                    for d in districts
                }
                total = sum(populations[node] for node in nodes)
                deviations_exact = [
                    abs(request.rules.district_count * district_populations[d] - total)
                    for d in districts
                ]
                values["max_population_deviation"] = max(deviations_exact)
                values["total_population_deviation"] = sum(deviations_exact)
            return values

        for stage_index, (stage_name, expression, sense) in enumerate(stages):
            if cancel_requested and cancel_requested():
                run.status = JobStatus.cancelled
                run.message = "Anulowano przed rozpoczęciem kolejnego etapu SCIP."
                return run
            stage_proof = self.artifact_dir / f"{run.id}-{stage_index:02d}-{stage_name}.vipr"
            model.setObjective(expression, sense)
            if self.require_exact:
                stage_model = self.artifact_dir / f"{run.id}-{stage_index:02d}-{stage_name}.cip"
                model.writeProblem(str(stage_model), verbose=False)
                model_paths.append(stage_model)
                exact_result = optimize_serialized(stage_model, stage_proof)
                proof_logging = bool(exact_result["proof_logging"])
                solve_status = str(exact_result["status"])
                solution_count = int(exact_result["nsols"])
            else:
                exact_result = None
                solved_model = model
                proof_logging = False
                solved_model.optimize()
                solve_status = str(solved_model.getStatus()).lower()
                solution_count = solved_model.getNSols()
            if solve_status == "infeasible" and stage_name == "target_seats":
                if self.require_exact and proof_logging and stage_proof.exists():
                    proof_entries.append((stage_name, stage_model, stage_proof))
                    manifest = self._verify_proofs(run, proof_entries, 1)
                    run.certificate_path = str(manifest)
                    verified = json.loads(manifest.read_text(encoding="utf-8"))["verified"]
                    run.certificate_verified = bool(verified)
                    run.status = JobStatus.infeasible if verified else JobStatus.failed
                    run.message = (
                        "Udowodniono brak rozwiązania i zweryfikowano certyfikat VIPR."
                        if verified
                        else "SCIP zgłosił brak rozwiązania, lecz VIPR nie potwierdził dowodu."
                    )
                else:
                    run.status = (
                        JobStatus.infeasible if not self.require_exact else JobStatus.failed
                    )
                    run.message = "SCIP zgłosił brak rozwiązania bez weryfikowalnego dowodu."
                return run
            if solve_status != "optimal":
                if cancel_requested and cancel_requested():
                    run.status = JobStatus.cancelled
                    run.message = f"Anulowano podczas etapu SCIP: {stage_name}."
                    return run
                run.status = (
                    JobStatus.feasible_checkpoint if solution_count else JobStatus.failed
                )
                run.message = (
                    f"SCIP: etap {stage_name} zakończył ze statusem {solve_status}; "
                    "wynik niecertyfikowany."
                )
                return run
            if exact_result is not None:
                stage_assignment = assignment_from_exact_result(exact_result)
                optimum = exact_integer_objectives(stage_assignment)[stage_name]
            else:
                optimum = int(round(solved_model.getObjVal()))
            stage_values[stage_name] = optimum
            if self.require_exact and stage_index == len(stages) - 1:
                exact_assignment = stage_assignment
            if proof_logging and stage_proof.exists():
                proof_entries.append((stage_name, stage_model, stage_proof))
            if stage_index < len(stages) - 1:
                if not self.require_exact:
                    model.freeTransform()
                model.addCons(expression == optimum, name=f"fix_{stage_name}")
                if stage_name == "max_population_deviation":
                    # The equality on max_popdev logically implies these UBs.
                    # Materializing them prevents SCIP's VIPR writer from
                    # weakening a deviation row with its obsolete wide bound.
                    model.chgVarUb(max_population_deviation, optimum)
                    for deviation in deviations.values():
                        model.chgVarUb(deviation, optimum)

        if self.require_exact:
            if exact_assignment is None:
                raise RuntimeError("SCIP exact nie zwrócił przypisania końcowego etapu")
            assignment = exact_assignment
        else:
            assignment = {
                node: next(d for d in districts if model.getVal(x[node, d]) > 0.5)
                for node in nodes
            }
        plan = self._build_plan(request, assignment, set(eligible), proportional)
        run.incumbent = plan
        run.best_bound = plan.target_seats
        run.status = JobStatus.optimal
        run.message = "Udowodniono leksykograficzne optimum: " + ", ".join(
            f"{name}={value}" for name, value in stage_values.items()
        )

        # Enumerate distinct plans with exactly the same complete lexicographic
        # score. Each extra solve is exact and receives its own proof.
        if request.alternatives > 1:
            if not self.require_exact:
                model.freeTransform()
            last_name, last_expression, _last_sense = stages[-1]
            model.addCons(last_expression == stage_values[last_name], name=f"fix_{last_name}")
            previous_assignments = [assignment]
            for alternative_index in range(1, request.alternatives):
                if cancel_requested and cancel_requested():
                    run.message += "; anulowano generowanie dalszych alternatyw."
                    break
                previous = previous_assignments[-1]
                model.addCons(
                    quicksum(x[node, previous[node]] for node in nodes) <= n - 1,
                    name=f"exclude_plan_{alternative_index}",
                )
                diversity = quicksum(1 - x[node, assignment[node]] for node in nodes)
                alternative_proof = self.artifact_dir / f"{run.id}-alt-{alternative_index:02d}.vipr"
                model.setObjective(diversity, "maximize")
                if self.require_exact:
                    alternative_model = self.artifact_dir / f"{run.id}-alt-{alternative_index:02d}.cip"
                    model.writeProblem(str(alternative_model), verbose=False)
                    model_paths.append(alternative_model)
                    candidate_result = optimize_serialized(
                        alternative_model, alternative_proof
                    )
                    proof_logging = bool(candidate_result["proof_logging"])
                    candidate_status = str(candidate_result["status"])
                else:
                    candidate_result = None
                    candidate_model = model
                    proof_logging = False
                    candidate_model.optimize()
                    candidate_status = str(candidate_model.getStatus()).lower()
                if candidate_status == "infeasible":
                    break
                if candidate_status != "optimal":
                    run.message += "; generowanie alternatyw przerwano bez wpływu na dowód optimum planu głównego."
                    break
                if candidate_result is not None:
                    alternative_assignment = assignment_from_exact_result(candidate_result)
                else:
                    alternative_assignment = {
                        node: next(d for d in districts if model.getVal(x[node, d]) > 0.5)
                        for node in nodes
                    }
                previous_assignments.append(alternative_assignment)
                alternative = self._build_plan(request, alternative_assignment, set(eligible), proportional)
                run.alternatives.append(alternative)
                if proof_logging and alternative_proof.exists():
                    proof_entries.append((
                        f"alternative_{alternative_index}",
                        alternative_model,
                        alternative_proof,
                    ))
                if alternative_index < request.alternatives - 1:
                    if not self.require_exact:
                        model.freeTransform()

        model_path = model_paths[-1] if model_paths else self.artifact_dir / f"{run.id}.cip"
        if not model_paths:
            model.writeProblem(str(model_path))
        # A CIP file is only the model. Every lexicographic stage has a separate
        # proof because a certificate of the last constrained solve alone would
        # not prove the optimum values fixed during earlier stages.
        manifest = self._verify_proofs(
            run, proof_entries, len(stages) + len(run.alternatives)
        )
        run.certificate_path = str(manifest)
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        run.certificate_verified = bool(
            self.require_exact and manifest_payload["verified"]
        )
        if not run.certificate_verified:
            run.status = JobStatus.feasible_checkpoint
            run.message = "Znaleziono rozwiązanie, lecz nie potwierdzono optimum w trybie EXACTSOLVE."
        return run

    @staticmethod
    def _build_plan(
        request: OptimizationRequest,
        assignment: dict[str, int],
        eligible: set[str],
        proportional: bool,
    ) -> DistrictPlan:
        votes_by_district: defaultdict[int, defaultdict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        populations: defaultdict[int, int] = defaultdict(int)
        candidate_district = assignment.get(request.candidate_anchor) if request.candidate_anchor else None
        for node, district in assignment.items():
            for party, value in request.scenario.votes_by_unit.get(node, {}).items():
                if (
                    request.target_kind == "candidate"
                    and party == request.target
                    and district != candidate_district
                ):
                    continue
                votes_by_district[district][party] += value
            populations[district] += request.scenario.population_by_unit.get(node, 0)
        seats = {}
        for district in range(request.rules.district_count):
            seats_d = (
                request.rules.seats_per_district[district]
                if isinstance(request.rules.seats_per_district, dict)
                else request.rules.seats_per_district
            )
            seats[district] = (
                dhondt(
                    {party: value for party, value in votes_by_district[district].items() if party in eligible},
                    seats_d,
                ).seats
                if proportional
                else plurality(dict(votes_by_district[district])).seats
            )
        total = sum(populations.values())
        ideal = total / request.rules.district_count if request.rules.district_count else 0
        population_deviation = (
            sum(abs(populations[district] - ideal) for district in range(request.rules.district_count)) / total
            if total else 0
        )
        changed = sum(
            request.base_assignment.get(node) != district
            for node, district in assignment.items()
        ) if request.base_assignment else 0
        plan = DistrictPlan(
            assignment=assignment,
            seats_by_district=seats,
            target_seats=sum(
                value.get(request.target, 0)
                for district, value in seats.items()
                if request.target_kind != "candidate" or district == candidate_district
            ),
            cut_border_m=cut_border(assignment, request.edges),
            population_deviation=population_deviation,
            changed_units=changed,
        )
        plan.validation = LegalValidator().validate(request, plan)
        return plan

    def _verify_proofs(
        self,
        run: OptimizationRun,
        entries: list[tuple[str, Path, Path]],
        expected: int,
    ) -> Path:
        viprcomp = shutil.which("viprcomp")
        viprchk = shutil.which("viprchk")
        records = []
        for label, model_path, raw in entries:
            complete = raw.with_name(f"{raw.stem}_complete.vipr")
            completed = checked = False
            detail = "Brak viprcomp/viprchk w PATH"
            if viprcomp and viprchk:
                completion = subprocess.run(
                    [viprcomp, str(raw)], capture_output=True, text=True, timeout=None, check=False
                )
                completed = completion.returncode == 0 and complete.exists()
                if completed:
                    verification = subprocess.run(
                        [viprchk, str(complete)], capture_output=True, text=True, timeout=None, check=False
                    )
                    checked = verification.returncode == 0
                    detail = (verification.stdout + verification.stderr)[-2000:]
                else:
                    detail = (completion.stdout + completion.stderr)[-2000:]
            artifact = complete if complete.exists() else raw
            records.append({
                "stage": label,
                "model_path": str(model_path.resolve()),
                "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
                "raw": str(raw.resolve()),
                "checked_artifact": str(artifact.resolve()),
                "proof_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "completed": completed, "verified": checked, "verifier_output": detail,
            })
        request_bytes = run.request.model_dump_json().encode("utf-8")
        payload = {
            "schema_version": 2,
            "algorithm": "scip-exact-lexicographic-vipr-v2",
            "run_id": str(run.id),
            "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
            "expected_stages": expected, "proofs": records,
            "verified": len(records) == expected and all(record["verified"] for record in records),
        }
        path = self.artifact_dir / f"{run.id}-certificate.json"
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
        return path


# Backwards-compatible explicit name for callers that only submit JOW jobs.
ScipJowSolver = ScipExactSolver
