from pathlib import Path

from gerry.domain import AdjacencyEdge, DistrictRules, JobStatus, OptimizationRequest, VoteScenario
from gerry.solver import ExactEnumerator, assignment_key, canonical_assignment


def small_request() -> OptimizationRequest:
    nodes = ["a", "b", "c", "d"]
    return OptimizationRequest(
        profile_id="generic-jow",
        target_kind="committee",
        target="A",
        nodes=nodes,
        edges=[
            AdjacencyEdge(source="a", target="b", shared_border_m=1),
            AdjacencyEdge(source="b", target="c", shared_border_m=1),
            AdjacencyEdge(source="c", target="d", shared_border_m=1),
        ],
        rules=DistrictRules(district_count=2, population_tolerance=0.01),
        scenario=VoteScenario(
            name="fixture",
            votes_by_unit={
                "a": {"A": 9, "B": 1}, "b": {"A": 1, "B": 9},
                "c": {"A": 9, "B": 1}, "d": {"A": 1, "B": 9},
            },
            population_by_unit={node: 100 for node in nodes},
        ),
        alternatives=3,
    )


def test_canonical_assignment_removes_label_symmetry():
    assert canonical_assignment({"a": 5, "b": 5, "c": 9}) == (0, 0, 1)
    assert canonical_assignment({"a": 1, "b": 1, "c": 0}) == (0, 0, 1)


def test_base_assignment_makes_district_labels_semantic():
    request = small_request()
    request.base_assignment = {"a": 0, "b": 0, "c": 1, "d": 1}
    first = {"a": 0, "b": 0, "c": 1, "d": 1}
    relabelled = {node: 1 - district for node, district in first.items()}

    assert canonical_assignment(first) == canonical_assignment(relabelled)
    assert assignment_key(request, first) != assignment_key(request, relabelled)


def test_exact_solver_proves_optimum_and_certificate(tmp_path: Path):
    run = ExactEnumerator(tmp_path).solve(small_request())
    assert run.status == JobStatus.optimal
    assert run.incumbent is not None
    assert run.incumbent.target_seats == 2
    assert run.certificate_verified
    assert Path(run.certificate_path).exists()


def test_formal_validator_recognizes_current_partition_under_renumbering(tmp_path: Path):
    request = small_request()
    request.profile_id = "pl-rada-gminy-do-20k@2026-07-15"
    request.container_by_node = {node: "gmina" for node in request.nodes}
    request.base_assignment = {"a": 8, "b": 8, "c": 3, "d": 3}
    run = ExactEnumerator(tmp_path).solve(request)
    assert run.incumbent.validation.formal_current.value == "COMPLIANT"
    assert run.incumbent.validation.structural.value == "COMPLIANT"


def test_exact_solver_proves_infeasible(tmp_path: Path):
    request = small_request()
    request.rules.population_tolerance = 0
    request.scenario.population_by_unit["a"] = 1000
    run = ExactEnumerator(tmp_path).solve(request)
    assert run.status == JobStatus.infeasible
    assert run.certificate_verified


def test_exact_solver_honors_cancellation(tmp_path: Path):
    run = ExactEnumerator(tmp_path).solve(small_request(), cancel_requested=lambda: True)
    assert run.status == JobStatus.cancelled
    assert not run.certificate_verified


def test_polish_profile_without_required_hierarchy_is_unverifiable(tmp_path: Path):
    request = small_request()
    request.profile_id = "pl-rada-gminy-do-20k@2026-07-15"
    run = ExactEnumerator(tmp_path).solve(request)
    assert run.status == JobStatus.optimal
    assert run.incumbent.validation.structural.value == "UNVERIFIABLE"
    assert run.incumbent.validation.formal_current.value == "REQUIRES_ENACTMENT"
