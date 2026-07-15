import geopandas as gpd
import pytest
from shapely.geometry import box

from gerry.assembly import assemble_districting_inputs, split_gmina_teryts
from gerry.domain import VoteScenario


SEJM = "pl-sejm@2026-07-15"
SENAT = "pl-senat@2026-07-15"
RADA_POWIATU = "pl-rada-powiatu@2026-07-15"
RADA_GMINY = "pl-rada-gminy-powyzej-20k@2026-07-15"


def _gminy() -> gpd.GeoDataFrame:
    # Powiat 0203 has two gminy, powiat 0204 one, plus a large Warsaw district.
    return gpd.GeoDataFrame(
        {"teryt": ["020301", "020302", "020401", "146502"]},
        geometry=[
            box(0, 0, 10, 10),
            box(10, 0, 20, 10),
            box(20, 0, 30, 10),
            box(30, 0, 40, 10),
        ],
        crs=2180,
    )


def _scenario() -> VoteScenario:
    return VoteScenario(
        name="src",
        votes_by_unit={
            "020301_1": {"A": 10, "B": 5},
            "020302_1": {"A": 3, "B": 7},
            "020401_1": {"A": 4, "C": 2},
            "146502_1": {"A": 9},
        },
        population_by_unit={
            "020301_1": 1000,
            "020302_1": 1000,
            "020401_1": 1000,
            "146502_1": 600000,
        },
    )


def test_sejm_assembles_powiat_nodes_over_whole_country():
    result = assemble_districting_inputs(
        profile_id=SEJM, scenario=_scenario(), gminy=_gminy()
    )
    assert result["unit_level"] == "powiat"
    assert sorted(result["graph"]["node_ids"]) == ["0203", "0204", "1465"]
    # Votes aggregated up to the powiat node.
    assert result["scenario"].votes_by_unit["0203"] == {"A": 13, "B": 12}
    nodes = {feature["properties"]["node"] for feature in result["geometry"]["features"]}
    assert nodes == {"0203", "0204", "1465"}
    # Sejm okręgi may not cross a województwo, so each powiat carries its 2-digit code.
    assert result["container_by_node"] == {"0203": "02", "0204": "02", "1465": "14"}


def test_senat_keeps_large_city_split_from_its_powiat():
    result = assemble_districting_inputs(
        profile_id=SENAT, scenario=_scenario(), gminy=_gminy()
    )
    # Warsaw district exceeds 500k residents, so it stays its own node.
    assert "146502" in result["graph"]["node_ids"]
    assert "1465" not in result["graph"]["node_ids"]


def test_rada_powiatu_scopes_to_one_powiat_with_gmina_nodes():
    result = assemble_districting_inputs(
        profile_id=RADA_POWIATU, scenario=_scenario(), gminy=_gminy(), unit="0203"
    )
    assert result["unit_level"] == "gmina"
    assert sorted(result["graph"]["node_ids"]) == ["020301", "020302"]
    assert result["scenario"].votes_by_unit["020301"] == {"A": 10, "B": 5}
    # Rada powiatu okręgi stay within the powiat: each gmina carries its 4-digit code.
    assert result["container_by_node"] == {"020301": "0203", "020302": "0203"}


def test_rada_powiatu_requires_unit():
    with pytest.raises(ValueError, match="wymaga wskazania jednostki"):
        assemble_districting_inputs(
            profile_id=RADA_POWIATU, scenario=_scenario(), gminy=_gminy()
        )


def test_rada_gminy_uses_precinct_layer_for_one_gmina():
    precincts = gpd.GeoDataFrame(
        {"key": ["020301_1", "020301_2", "020302_1"], "teryt": ["020301", "020301", "020302"]},
        geometry=[box(0, 0, 5, 10), box(5, 0, 10, 10), box(10, 0, 20, 10)],
        crs=2180,
    )
    scenario = VoteScenario(
        name="src",
        votes_by_unit={
            "020301_1": {"A": 1}, "020301_2": {"A": 2}, "020302_1": {"A": 3}
        },
    )
    result = assemble_districting_inputs(
        profile_id=RADA_GMINY, scenario=scenario, precincts=precincts, unit="020301"
    )
    assert result["unit_level"] == "precinct"
    assert sorted(result["graph"]["node_ids"]) == ["020301_1", "020301_2"]
    assert set(result["scenario"].votes_by_unit) == {"020301_1", "020301_2"}


def test_split_gmina_teryts_thresholds_population():
    assert split_gmina_teryts(_scenario(), 500000) == frozenset({"146502"})
    assert split_gmina_teryts(_scenario(), None) == frozenset()


def test_split_threshold_applies_at_powiat_level_not_per_district():
    # Two Warsaw districts each below 500k but their city (powiat 1465) exceeds it,
    # so both must be kept as separate nodes.
    scenario = VoteScenario(
        name="w",
        votes_by_unit={"146502_1": {"A": 1}, "146508_1": {"A": 1}, "020301_1": {"A": 1}},
        population_by_unit={"146502_1": 300000, "146508_1": 300000, "020301_1": 1000},
    )
    assert split_gmina_teryts(scenario, 500000) == frozenset({"146502", "146508"})
