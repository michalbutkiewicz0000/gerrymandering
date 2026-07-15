from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


class JobStatus(StrEnum):
    queued = "QUEUED"
    running = "RUNNING"
    feasible_checkpoint = "FEASIBLE_CHECKPOINT"
    optimal = "OPTIMAL"
    infeasible = "INFEASIBLE"
    cancelled = "CANCELLED"
    failed = "FAILED"
    objective_invariant = "OBJECTIVE_INVARIANT"


class ComplianceStatus(StrEnum):
    compliant = "COMPLIANT"
    non_compliant = "NON_COMPLIANT"
    unverifiable = "UNVERIFIABLE"
    requires_enactment = "REQUIRES_ENACTMENT"


class DataSnapshot(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    election_id: str
    effective_date: date
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    artifacts: list["SourceArtifact"] = Field(default_factory=list)
    status: Literal["CREATED", "READY", "FAILED"] = "CREATED"


class SourceArtifact(BaseModel):
    source: Literal["PKW", "PRG", "TERYT", "GUS", "BREC", "LOCAL"]
    url: str | None = None
    local_path: str
    sha256: str
    downloaded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Precinct(BaseModel):
    key: str
    snapshot_id: UUID | None = None
    teryt: str
    number: int
    name: str = ""
    boundary_description: str = ""
    commission_address: str = ""
    special: bool = False
    population: int | None = None
    eligible: int = 0
    votes: dict[str, int] = Field(default_factory=dict)
    geometry_quality: Literal["official", "generated", "approximate", "fallback", "none"] = "none"
    reconstruction: dict[str, Any] = Field(default_factory=dict)


class AdjacencyEdge(BaseModel):
    source: str
    target: str
    shared_border_m: float = Field(ge=0)
    kind: Literal["physical", "bridge", "ferry"] = "physical"

    @model_validator(mode="after")
    def normalize(self) -> "AdjacencyEdge":
        if self.source == self.target:
            raise ValueError("self-loops are forbidden")
        if self.target < self.source:
            self.source, self.target = self.target, self.source
        return self


class VoteScenario(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    snapshot_id: UUID | None = None
    votes_by_unit: dict[str, dict[str, int]]
    eligible_by_unit: dict[str, int] = Field(default_factory=dict)
    population_by_unit: dict[str, int] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=dict)
    threshold_exempt: set[str] = Field(default_factory=set)

    @model_validator(mode="after")
    def non_negative_election_data(self) -> "VoteScenario":
        for unit, votes in self.votes_by_unit.items():
            if any(value < 0 for value in votes.values()):
                raise ValueError(f"votes must be non-negative: {unit}")
        for name, values in (
            ("eligible_by_unit", self.eligible_by_unit),
            ("population_by_unit", self.population_by_unit),
        ):
            if any(value < 0 for value in values.values()):
                raise ValueError(f"{name} values must be non-negative")
        if any(value < 0 or value > 1 for value in self.thresholds.values()):
            raise ValueError("thresholds must be between 0 and 1")
        return self


class DistrictRules(BaseModel):
    district_count: int = Field(gt=0)
    seats_per_district: int | dict[int, int] = 1
    population_tolerance: float = Field(default=0.10, ge=0, lt=1)
    allowed_edge_kinds: set[Literal["physical", "bridge", "ferry"]] = Field(
        default_factory=lambda: {"physical"}
    )
    max_cut_border_m: float | None = None
    indivisible_parent_level: str | None = None

    @model_validator(mode="after")
    def valid_seat_configuration(self) -> "DistrictRules":
        if isinstance(self.seats_per_district, int):
            if self.seats_per_district <= 0:
                raise ValueError("seats_per_district must be positive")
        else:
            expected = set(range(self.district_count))
            if set(self.seats_per_district) != expected:
                raise ValueError(
                    "seats_per_district must define exactly districts 0..district_count-1"
                )
            if any(value <= 0 for value in self.seats_per_district.values()):
                raise ValueError("seats_per_district values must be positive")
        if self.max_cut_border_m is not None and self.max_cut_border_m < 0:
            raise ValueError("max_cut_border_m must be non-negative")
        return self


class OptimizationRequest(BaseModel):
    profile_id: str
    target_kind: Literal["committee", "candidate"]
    target: str
    scenario: VoteScenario
    rules: DistrictRules
    nodes: list[str]
    edges: list[AdjacencyEdge]
    base_assignment: dict[str, int] | None = None
    parent_by_node: dict[str, str] = Field(default_factory=dict)
    container_by_node: dict[str, str] = Field(default_factory=dict)
    geometry_by_node: dict[str, dict[str, Any]] = Field(default_factory=dict)
    candidate_anchor: str | None = None
    alternatives: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def candidate_rules(self) -> "OptimizationRequest":
        if not self.nodes:
            raise ValueError("nodes must not be empty")
        if len(set(self.nodes)) != len(self.nodes):
            raise ValueError("nodes must be unique")
        known = set(self.nodes)
        seen_edges: set[tuple[str, str]] = set()
        for edge in self.edges:
            if edge.source not in known or edge.target not in known:
                raise ValueError("every edge endpoint must be one of nodes")
            key = (edge.source, edge.target)
            if key in seen_edges:
                raise ValueError("duplicate edges are forbidden")
            seen_edges.add(key)
        for mapping_name, mapping in (
            ("base_assignment", self.base_assignment or {}),
            ("parent_by_node", self.parent_by_node),
            ("container_by_node", self.container_by_node),
            ("geometry_by_node", self.geometry_by_node),
        ):
            unknown = set(mapping) - known
            if unknown:
                raise ValueError(f"{mapping_name} contains unknown nodes: {sorted(unknown)}")
        if self.target_kind == "candidate" and not self.candidate_anchor:
            raise ValueError("candidate optimization requires candidate_anchor")
        if self.candidate_anchor and self.candidate_anchor not in self.nodes:
            raise ValueError("candidate_anchor must be one of nodes")
        return self


class ValidationFinding(BaseModel):
    code: str
    status: ComplianceStatus
    message: str
    citation: str | None = None
    measured: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    structural: ComplianceStatus
    formal_current: ComplianceStatus
    findings: list[ValidationFinding]
    law_snapshot: date
    law_profile_sha256: str
    law_sources: list[str] = Field(default_factory=list)


class DistrictPlan(BaseModel):
    assignment: dict[str, int]
    seats_by_district: dict[int, dict[str, int]] = Field(default_factory=dict)
    target_seats: int
    cut_border_m: float
    population_deviation: float
    changed_units: int = 0
    validation: ValidationReport | None = None


class OptimizationRun(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    status: JobStatus = JobStatus.queued
    request: OptimizationRequest
    incumbent: DistrictPlan | None = None
    alternatives: list[DistrictPlan] = Field(default_factory=list)
    best_bound: int | None = None
    certificate_path: str | None = None
    certificate_verified: bool = False
    message: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
