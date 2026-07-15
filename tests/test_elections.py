from gerry.elections import apply_thresholds, dhondt, european_parliament_committee_seats, plurality


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
