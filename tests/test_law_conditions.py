from gerry.domain import (
    AdjacencyEdge,
    DistrictPlan,
    DistrictRules,
    OptimizationRequest,
    VoteScenario,
)
from gerry.law import LegalValidator


def county_request() -> OptimizationRequest:
    nodes = ["a", "b", "c", "d"]
    return OptimizationRequest(
        profile_id="pl-rada-powiatu@2026-07-15",
        target_kind="committee",
        target="A",
        nodes=nodes,
        edges=[
            AdjacencyEdge(source="a", target="c", shared_border_m=1),
            AdjacencyEdge(source="c", target="d", shared_border_m=1),
            AdjacencyEdge(source="d", target="b", shared_border_m=1),
        ],
        parent_by_node={"a": "large", "b": "large", "c": "small", "d": "small"},
        container_by_node={node: "county" for node in nodes},
        scenario=VoteScenario(
            name="norm",
            votes_by_unit={node: {"A": 1} for node in nodes},
            population_by_unit={"a": 400, "b": 400, "c": 100, "d": 100},
        ),
        rules=DistrictRules(
            district_count=3, seats_per_district=8, population_tolerance=0.9
        ),
    )


def plan(assignment: dict[str, int]) -> DistrictPlan:
    return DistrictPlan(
        assignment=assignment,
        target_seats=3,
        cut_border_m=0,
        population_deviation=0,
    )


def test_parent_may_be_split_only_above_statutory_entitlement():
    request = county_request()
    allowed = LegalValidator().validate(
        request, plan({"a": 0, "b": 1, "c": 2, "d": 2})
    )
    finding = next(item for item in allowed.findings if item.code == "INDIVISIBLE_PARENTS")
    assert finding.status.value == "COMPLIANT"
    assert finding.measured["allowed_split"] == ["large"]

    forbidden = LegalValidator().validate(
        request, plan({"a": 0, "b": 1, "c": 0, "d": 2})
    )
    finding = next(item for item in forbidden.findings if item.code == "INDIVISIBLE_PARENTS")
    assert finding.status.value == "NON_COMPLIANT"
    assert finding.measured["illegal_split"] == ["small"]
    assert len(forbidden.law_profile_sha256) == 64
    assert forbidden.law_sources
    assert all("https://eli.gov.pl/" in source for source in forbidden.law_sources)


def test_configured_population_balance_is_not_mislabelled_as_statutory_rule():
    request = county_request()
    report = LegalValidator().validate(
        request, plan({"a": 0, "b": 0, "c": 1, "d": 2})
    )
    finding = next(
        item for item in report.findings
        if item.code == "CONFIGURED_POPULATION_BALANCE"
    )

    assert finding.citation is None
    assert "nie zastępuje ustawowego algorytmu" in finding.message
