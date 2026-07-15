import pytest
from pydantic import ValidationError

from gerry.domain import (
    AdjacencyEdge,
    DistrictRules,
    OptimizationRequest,
    VoteScenario,
)


def scenario() -> VoteScenario:
    return VoteScenario(
        name="input-validation",
        votes_by_unit={"a": {"A": 1}, "b": {"A": 2}},
        population_by_unit={"a": 10, "b": 10},
    )


def request(**changes) -> OptimizationRequest:
    values = {
        "profile_id": "generic-jow",
        "target_kind": "committee",
        "target": "A",
        "scenario": scenario(),
        "rules": DistrictRules(district_count=1),
        "nodes": ["a", "b"],
        "edges": [AdjacencyEdge(source="a", target="b", shared_border_m=1)],
    }
    values.update(changes)
    return OptimizationRequest(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("votes_by_unit", {"a": {"A": -1}}),
        ("population_by_unit", {"a": -1}),
        ("eligible_by_unit", {"a": -1}),
        ("thresholds", {"A": 1.01}),
    ],
)
def test_scenario_rejects_negative_or_out_of_range_election_data(field, value):
    data = {"name": "bad", "votes_by_unit": {"a": {"A": 1}}, field: value}
    with pytest.raises(ValidationError):
        VoteScenario(**data)


def test_rules_require_positive_complete_seat_configuration():
    with pytest.raises(ValidationError, match="district_count"):
        DistrictRules(district_count=0)
    with pytest.raises(ValidationError, match="define exactly"):
        DistrictRules(district_count=2, seats_per_district={0: 1})
    with pytest.raises(ValidationError, match="positive"):
        DistrictRules(district_count=1, seats_per_district=0)


@pytest.mark.parametrize(
    "changes",
    [
        {"nodes": ["a", "a"]},
        {
            "edges": [
                AdjacencyEdge(source="a", target="missing", shared_border_m=1)
            ]
        },
        {
            "edges": [
                AdjacencyEdge(source="a", target="b", shared_border_m=1),
                AdjacencyEdge(source="b", target="a", shared_border_m=2),
            ]
        },
        {"parent_by_node": {"missing": "parent"}},
    ],
)
def test_request_rejects_ambiguous_or_unknown_graph_data(changes):
    with pytest.raises(ValidationError):
        request(**changes)


def test_base_partition_labels_may_be_arbitrary_current_district_numbers():
    parsed = request(base_assignment={"a": 8, "b": 3})
    assert parsed.base_assignment == {"a": 8, "b": 3}
