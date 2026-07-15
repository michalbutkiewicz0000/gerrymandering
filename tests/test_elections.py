from gerry.domain import VoteScenario
from gerry.elections import (
    aggregate_scenario,
    aggregation_key,
    apply_thresholds,
    dhondt,
    european_parliament_committee_seats,
    plurality,
)


def _precinct_scenario() -> VoteScenario:
    # Two precincts in gmina 020301 (powiat 0203), one in gmina 020302 (powiat 0203),
    # and one Warsaw district 146502 (powiat 1465).
    return VoteScenario(
        name="test",
        votes_by_unit={
            "020301_1": {"A": 10, "B": 5},
            "020301_2": {"A": 3, "B": 7},
            "020302_1": {"A": 1, "C": 2},
            "146502_1": {"A": 4},
        },
        eligible_by_unit={"020301_1": 20, "020301_2": 15, "020302_1": 5, "146502_1": 8},
        population_by_unit={"020301_1": 30, "020301_2": 25, "020302_1": 9, "146502_1": 12},
    )


def test_dhondt_uses_exact_fraction_arithmetic():
    result = dhondt({"A": 100, "B": 80, "C": 30}, 5)
    assert result.seats == {"A": 3, "B": 2}


def test_plurality_marks_tie_and_is_deterministic():
    result = plurality({"B": 10, "A": 10})
    assert result.seats == {"A": 1}
    assert result.tie


def test_thresholds_and_eu_total_are_boundary_invariant():
    votes = {"A": 50, "B": 4, "C": 46}
    assert apply_thresholds(votes, {party: 0.05 for party in votes}) == {"A": 50, "C": 46}
    assert sum(european_parliament_committee_seats(votes, 10).seats.values()) == 10


def test_aggregation_key_maps_to_requested_level():
    assert aggregation_key("020301_7", "precinct") == "020301_7"
    assert aggregation_key("020301_7", "gmina") == "020301"
    assert aggregation_key("020301_7", "powiat") == "0203"
    # Warsaw district stays at gmina granularity when explicitly kept.
    assert aggregation_key("146502_1", "powiat", frozenset({"146502"})) == "146502"


def test_aggregate_scenario_preserves_vote_totals():
    scenario = _precinct_scenario()
    powiat = aggregate_scenario(scenario, "powiat")
    assert powiat.votes_by_unit == {
        "0203": {"A": 14, "B": 12, "C": 2},
        "1465": {"A": 4},
    }
    assert powiat.eligible_by_unit == {"0203": 40, "1465": 8}
    assert powiat.population_by_unit == {"0203": 64, "1465": 12}
    # Every committee's national total is invariant under aggregation.
    def committee_total(units):
        totals: dict[str, int] = {}
        for row in units.values():
            for party, value in row.items():
                totals[party] = totals.get(party, 0) + value
        return totals
    assert committee_total(powiat.votes_by_unit) == committee_total(scenario.votes_by_unit)
    assert powiat.id != scenario.id


def test_aggregate_scenario_to_gmina_and_warsaw_split():
    scenario = _precinct_scenario()
    gmina = aggregate_scenario(scenario, "gmina")
    assert set(gmina.votes_by_unit) == {"020301", "020302", "146502"}
    assert gmina.votes_by_unit["020301"] == {"A": 13, "B": 12}
    # Senate keeps Warsaw split while collapsing the rest to powiat.
    senate = aggregate_scenario(scenario, "powiat", keep_gmina=frozenset({"146502"}))
    assert set(senate.votes_by_unit) == {"0203", "146502"}


def test_aggregate_scenario_precinct_is_identity():
    scenario = _precinct_scenario()
    assert aggregate_scenario(scenario, "precinct") is scenario
