from __future__ import annotations

import json
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import pandas as pd

from .domain import VoteScenario
from .sources import UNIT_LEVELS, node_key_for_teryt, normalize_precinct, normalize_teryt

__all__ = [
    "UNIT_LEVELS",
    "aggregation_key",
    "aggregate_scenario",
    "scenario_from_pkw",
    "allocate",
    "dhondt",
    "plurality",
    "apply_thresholds",
]


def aggregation_key(unit_key: str, level: str, keep_gmina: frozenset[str] = frozenset()) -> str:
    """Map a precinct key ``{teryt}_{precinct}`` to its node at ``level``.

    A precinct key carries the 6-digit gmina TERYT before the underscore, which
    :func:`gerry.sources.node_key_for_teryt` collapses to the gmina or powiat node;
    ``keep_gmina`` forwards the Senate's Warsaw-split exception.
    """
    if level == "precinct":
        return unit_key
    return node_key_for_teryt(unit_key.split("_", 1)[0], level, keep_gmina)


def aggregate_scenario(
    scenario: VoteScenario, level: str, *, keep_gmina: frozenset[str] = frozenset()
) -> VoteScenario:
    """Sum a precinct scenario's votes, electors and population up to ``level``.

    Boundaries never move votes between committees, so the aggregate is the exact
    row-wise sum grouped by :func:`aggregation_key`. Returns the scenario unchanged
    for the ``precinct`` level, otherwise a copy with a fresh id keyed at ``level``.
    """
    if level == "precinct":
        return scenario
    votes: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
    eligible: defaultdict[str, int] = defaultdict(int)
    populations: defaultdict[str, int] = defaultdict(int)
    for unit, row in scenario.votes_by_unit.items():
        key = aggregation_key(unit, level, keep_gmina)
        for party, value in row.items():
            votes[key][party] += value
    for unit, value in scenario.eligible_by_unit.items():
        eligible[aggregation_key(unit, level, keep_gmina)] += value
    for unit, value in scenario.population_by_unit.items():
        populations[aggregation_key(unit, level, keep_gmina)] += value
    return scenario.model_copy(
        update={
            "id": uuid4(),
            "votes_by_unit": {key: dict(row) for key, row in votes.items()},
            "eligible_by_unit": dict(eligible),
            "population_by_unit": dict(populations),
        }
    )


@dataclass(frozen=True)
class AllocationResult:
    seats: dict[str, int]
    tie: bool = False


def plurality(votes: dict[str, int]) -> AllocationResult:
    if not votes:
        return AllocationResult({})
    best = max(votes.values())
    winners = sorted(name for name, value in votes.items() if value == best)
    return AllocationResult({winners[0]: 1}, tie=len(winners) > 1)


def dhondt(votes: dict[str, int], seats: int) -> AllocationResult:
    if seats < 0:
        raise ValueError("seats must be non-negative")
    allocated: defaultdict[str, int] = defaultdict(int)
    tie = False
    for _ in range(seats):
        quotients = {name: Fraction(value, allocated[name] + 1) for name, value in votes.items()}
        if not quotients:
            break
        best = max(quotients.values())
        winners = sorted(name for name, quotient in quotients.items() if quotient == best)
        tie |= len(winners) > 1
        allocated[winners[0]] += 1
    return AllocationResult({name: value for name, value in allocated.items() if value > 0}, tie=tie)


def apply_thresholds(
    votes: dict[str, int], thresholds: dict[str, float], exempt: set[str] | None = None
) -> dict[str, int]:
    exempt = exempt or set()
    total = sum(votes.values())
    if total <= 0:
        return votes.copy()
    return {
        party: value
        for party, value in votes.items()
        if party in exempt
        or Fraction(value, total) >= Fraction(str(thresholds.get(party, 0.0)))
    }


def dhondt_with_thresholds(
    votes: dict[str, int], seats: int, thresholds: dict[str, float], exempt: set[str] | None = None
) -> AllocationResult:
    return dhondt(apply_thresholds(votes, thresholds, exempt), seats)


def european_parliament_committee_seats(
    national_votes: dict[str, int], total_seats: int, thresholds: dict[str, float] | None = None
) -> AllocationResult:
    """Committee-level Polish EP allocation; district boundaries cannot change this total."""
    thresholds = thresholds or {party: 0.05 for party in national_votes}
    return dhondt_with_thresholds(national_votes, total_seats, thresholds)


def allocate(method: str, votes: dict[str, int], seats: int) -> AllocationResult:
    if method == "plurality":
        return plurality(votes)
    if method == "dhondt":
        return dhondt(votes, seats)
    raise ValueError(f"unsupported allocation method: {method}")


def scenario_from_pkw(
    path: Path,
    name: str,
    *,
    attachments_path: Path | None = None,
    vote_columns: list[str] | None = None,
    thresholds: dict[str, float] | None = None,
    threshold_exempt: set[str] | None = None,
    unit_level: str = "precinct",
) -> VoteScenario:
    """Import a wide PKW commission-results sheet into an auditable scenario.

    All numeric columns not recognized as metadata are treated as committee or
    candidate vote columns. Repeated commission rows are summed. An optional
    reconstruction attachment table moves non-territorial votes to host nodes.
    With ``unit_level`` other than ``precinct`` the precinct rows are aggregated
    up to gmina or powiat nodes, the granularity most elections actually model.
    """
    if path.suffix.lower() in {".xlsx", ".xls"}:
        frame = pd.read_excel(path, dtype=str)
    elif path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            candidates = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(candidates) != 1:
                raise ValueError("Archiwum wyników ZIP musi zawierać dokładnie jeden plik CSV.")
            with archive.open(candidates[0]) as handle:
                frame = pd.read_csv(handle, dtype=str, sep=None, engine="python")
    else:
        frame = pd.read_csv(path, dtype=str, sep=None, engine="python")
    normalized = {
        str(column).lstrip("\ufeff").strip().strip('"').lower(): column
        for column in frame.columns
    }

    def find(*aliases: str) -> str | None:
        return next((normalized[alias] for alias in aliases if alias in normalized), None)

    teryt_column = find("teryt", "teryt gminy", "kod teryt", "kod gminy")
    precinct_column = find(
        "numer obwodu", "nr obwodu", "nr komisji", "numer komisji", "numer", "obwód", "obwod"
    )
    if not teryt_column or not precinct_column:
        raise ValueError("Wyniki PKW muszą zawierać TERYT i numer obwodu.")
    eligible_column = find(
        "liczba uprawnionych", "uprawnieni", "wyborcy", "liczba wyborców",
        "liczba wyborców uprawnionych do głosowania",
    )
    population_column = find("ludność", "ludnosc", "populacja")
    metadata = {
        column for column in (
            teryt_column, precinct_column, eligible_column, population_column,
            find("województwo", "wojewodztwo"), find("powiat"), find("gmina"),
            find("siedziba"), find("adres"), find("typ obwodu"),
        ) if column is not None
    }
    requested_vote_columns = set(vote_columns or [])
    missing_vote_columns = requested_vote_columns - set(map(str, frame.columns))
    if missing_vote_columns:
        raise ValueError(f"Brak wskazanych kolumn głosów: {', '.join(sorted(missing_vote_columns))}")
    official_committee_columns = {
        str(column)
        for column in frame.columns
        if str(column).strip().upper().startswith((
            "KOMITET WYBORCZY ",
            "KOALICYJNY KOMITET WYBORCZY ",
        ))
    }
    numeric = {}
    for column in frame.columns:
        if column in metadata:
            continue
        if requested_vote_columns and str(column) not in requested_vote_columns:
            continue
        if (
            not requested_vote_columns
            and official_committee_columns
            and str(column) not in official_committee_columns
        ):
            continue
        values = pd.to_numeric(frame[column].astype(str).str.replace(" ", ""), errors="coerce")
        if values.notna().any():
            numeric[str(column).strip()] = values.fillna(0).astype(int)
    if not numeric:
        raise ValueError("Nie wykryto kolumn z głosami komitetów lub kandydatów.")

    attachments: dict[str, str] = {}
    if attachments_path:
        attachment_frame = (
            pd.DataFrame(json.loads(attachments_path.read_text(encoding="utf-8")))
            if attachments_path.suffix == ".json"
            else pd.read_csv(attachments_path, dtype=str)
        )
        required = {"special_key", "host_key"}
        if not attachment_frame.empty and not required <= set(attachment_frame.columns):
            raise ValueError("Tabela przyłączeń wymaga kolumn special_key i host_key.")
        if not attachment_frame.empty:
            attachments = dict(zip(
                attachment_frame.special_key.astype(str),
                attachment_frame.host_key.astype(str),
            ))

    votes: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
    eligible: defaultdict[str, int] = defaultdict(int)
    populations: defaultdict[str, int] = defaultdict(int)
    for index, row in frame.iterrows():
        teryt = normalize_teryt(row[teryt_column])
        precinct = normalize_precinct(row[precinct_column])
        if not teryt or precinct is None:
            continue
        raw_key = f"{teryt}_{precinct}"
        key = attachments.get(raw_key, raw_key)
        for party, values in numeric.items():
            votes[key][party] += int(values.loc[index])
        if eligible_column:
            value = pd.to_numeric(row[eligible_column], errors="coerce")
            eligible[key] += 0 if pd.isna(value) else int(value)
        if population_column:
            value = pd.to_numeric(row[population_column], errors="coerce")
            populations[key] += 0 if pd.isna(value) else int(value)
    scenario = VoteScenario(
        name=name,
        votes_by_unit={key: dict(values) for key, values in votes.items()},
        eligible_by_unit=dict(eligible),
        population_by_unit=dict(populations),
        thresholds=thresholds or {},
        threshold_exempt=threshold_exempt or set(),
    )
    return aggregate_scenario(scenario, unit_level)
