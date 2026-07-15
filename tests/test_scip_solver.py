import hashlib
import json
import random
from types import SimpleNamespace

from gerry.domain import AdjacencyEdge, DistrictRules, JobStatus, OptimizationRequest, VoteScenario
from gerry.graph import as_networkx
from gerry.solver import ExactEnumerator
import pytest

from gerry.scip_solver import (
    ScipJowSolver,
    ScipUnavailable,
    exact_scip_available,
    verify_vipr_manifest,
)

from test_solver import small_request
from test_law_conditions import county_request


def test_scip_jow_matches_exhaustive_oracle(tmp_path):
    run = ScipJowSolver(tmp_path, require_exact=False).solve(small_request())
    assert run.status == JobStatus.feasible_checkpoint
    assert run.incumbent.target_seats == 2
    assert run.incumbent.validation.structural.value == "COMPLIANT"
    assert not run.certificate_verified


def test_scip_proportional_matches_exhaustive_oracle(tmp_path):
    nodes = ["a", "b", "c", "d"]
    request = OptimizationRequest(
        profile_id="generic-proportional", target_kind="committee", target="A", nodes=nodes,
        edges=[
            AdjacencyEdge(source="a", target="b", shared_border_m=1),
            AdjacencyEdge(source="b", target="c", shared_border_m=1),
            AdjacencyEdge(source="c", target="d", shared_border_m=1),
        ],
        rules=DistrictRules(district_count=2, seats_per_district=3, population_tolerance=0),
        scenario=VoteScenario(
            name="proportional", population_by_unit={node: 100 for node in nodes},
            votes_by_unit={
                "a": {"A": 60, "B": 40}, "b": {"A": 10, "B": 90},
                "c": {"A": 60, "B": 40}, "d": {"A": 10, "B": 90},
            },
        ), alternatives=1,
    )
    oracle = ExactEnumerator(tmp_path / "oracle").solve(request)
    scip = ScipJowSolver(tmp_path / "scip", require_exact=False).solve(request)
    assert oracle.status == JobStatus.optimal
    assert scip.status == JobStatus.feasible_checkpoint
    assert oracle.incumbent.target_seats == scip.incumbent.target_seats


def test_scip_variable_names_stay_whitespace_free_for_spaced_committees(tmp_path, monkeypatch):
    # Real PKW committee names carry spaces; the VIPR certificate writer refuses
    # variable names with whitespace, so no SCIP variable may embed the raw name.
    import pyscipopt

    seen_names: list[str] = []

    class RecordingModel(pyscipopt.Model):
        def addVar(self, *args, **kwargs):
            if "name" in kwargs:
                seen_names.append(kwargs["name"])
            return super().addVar(*args, **kwargs)

    monkeypatch.setattr(pyscipopt, "Model", RecordingModel)
    committee = "KOALICYJNY KOMITET WYBORCZY KOALICJA OBYWATELSKA PO .N IPL ZIELONI"
    nodes = ["a", "b", "c", "d"]
    request = OptimizationRequest(
        profile_id="generic-proportional", target_kind="committee", target=committee, nodes=nodes,
        edges=[
            AdjacencyEdge(source="a", target="b", shared_border_m=1),
            AdjacencyEdge(source="b", target="c", shared_border_m=1),
            AdjacencyEdge(source="c", target="d", shared_border_m=1),
        ],
        rules=DistrictRules(district_count=2, seats_per_district=3, population_tolerance=0),
        scenario=VoteScenario(
            name="spaced", population_by_unit={node: 100 for node in nodes},
            votes_by_unit={
                "a": {committee: 60, "B": 40}, "b": {committee: 10, "B": 90},
                "c": {committee: 60, "B": 40}, "d": {committee: 10, "B": 90},
            },
        ), alternatives=1,
    )

    run = ScipJowSolver(tmp_path, require_exact=False).solve(request)

    assert run.status == JobStatus.feasible_checkpoint
    assert seen_names, "the model must create named variables"
    offenders = [name for name in seen_names if any(character.isspace() for character in name)]
    assert offenders == []


def test_non_exact_scip_is_never_reported_as_certified(tmp_path):
    if exact_scip_available()[0]:
        pytest.skip("test dotyczy standardowego koła PyPI bez EXACTSOLVE")
    with pytest.raises(ScipUnavailable, match="EXACTSOLVE"):
        ScipJowSolver(tmp_path).solve(small_request())


@pytest.mark.skipif(not exact_scip_available()[0], reason="wymaga obrazu SCIP EXACTSOLVE")
def test_exact_scip_produces_verified_vipr_manifest(tmp_path):
    run = ScipJowSolver(tmp_path).solve(small_request())
    assert run.status == JobStatus.optimal
    assert run.certificate_verified
    assert run.certificate_path.endswith("-certificate.json")
    manifest = json.loads(open(run.certificate_path, encoding="utf-8").read())
    assert manifest["schema_version"] == 2
    assert manifest["verified"] is True
    assert len(manifest["proofs"]) == manifest["expected_stages"]
    assert all(record["model_sha256"] for record in manifest["proofs"])
    assert all(record["proof_sha256"] for record in manifest["proofs"])


def test_vipr_manifest_binds_each_proof_to_its_model_and_request(tmp_path, monkeypatch):
    request = small_request()
    solver = ScipJowSolver(tmp_path, require_exact=True)
    run = SimpleNamespace(id="audit-run", request=request)
    model_a = tmp_path / "stage-a.cip"
    model_b = tmp_path / "stage-b.cip"
    proof_a = tmp_path / "stage-a.vipr"
    proof_b = tmp_path / "stage-b.vipr"
    model_a.write_bytes(b"model-a")
    model_b.write_bytes(b"model-b")
    proof_a.write_bytes(b"proof-a")
    proof_b.write_bytes(b"proof-b")

    monkeypatch.setattr("gerry.scip_solver.shutil.which", lambda name: f"/bin/{name}")

    def successful_tool(command, **kwargs):
        del kwargs
        if command[0].endswith("viprcomp"):
            raw = tmp_path / command[1]
            raw.with_name(f"{raw.stem}_complete.vipr").write_bytes(
                b"complete-" + raw.read_bytes()
            )
        return SimpleNamespace(returncode=0, stdout="verified", stderr="")

    monkeypatch.setattr("gerry.scip_solver.subprocess.run", successful_tool)
    manifest_path = solver._verify_proofs(
        run,
        [("target_seats", model_a, proof_a), ("cut_border_mm", model_b, proof_b)],
        2,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["verified"] is True
    assert manifest["schema_version"] == 2
    assert manifest["request_sha256"] == hashlib.sha256(
        request.model_dump_json().encode("utf-8")
    ).hexdigest()
    assert [record["stage"] for record in manifest["proofs"]] == [
        "target_seats", "cut_border_mm"
    ]
    assert manifest["proofs"][0]["model_sha256"] == hashlib.sha256(b"model-a").hexdigest()
    assert manifest["proofs"][1]["model_sha256"] == hashlib.sha256(b"model-b").hexdigest()
    assert manifest["proofs"][0]["proof_sha256"] == hashlib.sha256(
        b"complete-proof-a"
    ).hexdigest()
    verified, detail = verify_vipr_manifest(manifest_path, request)
    assert verified, detail

    model_a.write_bytes(b"tampered-model")
    verified, detail = verify_vipr_manifest(manifest_path, request)
    assert not verified
    assert "Niezgodna suma modelu" in detail


def test_scip_respects_conditional_parent_split_rules(tmp_path):
    request = county_request()
    request.alternatives = 1
    oracle = ExactEnumerator(tmp_path / "oracle").solve(request)
    scip = ScipJowSolver(tmp_path / "scip", require_exact=False).solve(request)
    assert oracle.incumbent is not None
    assert scip.incumbent is not None
    assert oracle.incumbent.target_seats == scip.incumbent.target_seats
    assert scip.incumbent.validation.structural.value != "NON_COMPLIANT"


@pytest.mark.parametrize("profile", ["generic-jow", "generic-proportional"])
@pytest.mark.parametrize("seed", range(3))
def test_scip_matches_exhaustive_full_lexicographic_objective(tmp_path, profile, seed):
    rng = random.Random(seed)
    nodes = [f"n{index}" for index in range(6)]
    edges = [
        AdjacencyEdge(
            source=nodes[index], target=nodes[(index + 1) % len(nodes)],
            shared_border_m=rng.randint(1, 20) / 3,
        )
        for index in range(len(nodes))
    ]
    edges.append(AdjacencyEdge(source="n1", target="n4", shared_border_m=2.75))
    request = OptimizationRequest(
        profile_id=profile,
        target_kind="committee",
        target="A",
        nodes=nodes,
        edges=edges,
        rules=DistrictRules(
            district_count=2,
            seats_per_district=1 if profile == "generic-jow" else 3,
            population_tolerance=0.34,
        ),
        scenario=VoteScenario(
            name=f"random-{seed}",
            votes_by_unit={
                node: {"A": rng.randint(1, 100), "B": rng.randint(1, 100), "C": rng.randint(1, 100)}
                for node in nodes
            },
            population_by_unit={node: 100 for node in nodes},
        ),
        base_assignment={node: index % 2 for index, node in enumerate(nodes)},
        alternatives=1,
    )
    oracle_solver = ExactEnumerator(tmp_path / f"oracle-{profile}-{seed}")
    oracle = oracle_solver.solve(request)
    scip = ScipJowSolver(
        tmp_path / f"scip-{profile}-{seed}", require_exact=False
    ).solve(request)

    assert oracle.incumbent is not None
    assert scip.incumbent is not None
    graph = as_networkx(
        request.nodes, request.edges, request.rules.allowed_edge_kinds
    )
    assert oracle_solver._objective(request, oracle.incumbent, graph) == oracle_solver._objective(
        request, scip.incumbent, graph
    )
