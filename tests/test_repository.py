from gerry.domain import JobStatus, OptimizationRun
from gerry.repository import RunRepository
from gerry.services import OptimizationService

from test_solver import small_request


def test_file_queue_claims_each_job_once(tmp_path):
    repository = RunRepository(tmp_path / "runs")
    first = OptimizationRun(request=small_request())
    second = OptimizationRun(request=small_request())
    repository.save(first)
    repository.save(second)
    claimed = {repository.claim_next().id, repository.claim_next().id}
    assert claimed == {first.id, second.id}
    assert repository.claim_next() is None
    assert all(repository.get(run_id).status == JobStatus.running for run_id in claimed)
    repository.healthcheck()
    assert not (tmp_path / "runs/.healthcheck").exists()


def test_file_repository_lists_newest_runs_with_pagination(tmp_path):
    repository = RunRepository(tmp_path / "runs")
    runs = [OptimizationRun(request=small_request()) for _ in range(4)]
    for index, run in enumerate(runs):
        run.created_at = run.created_at.replace(microsecond=index)
        repository.save(run)

    assert [run.id for run in repository.list(limit=2)] == [runs[3].id, runs[2].id]
    assert [run.id for run in repository.list(limit=2, offset=2)] == [runs[1].id, runs[0].id]


def test_cancelled_job_is_not_published_after_solver_returns(tmp_path, monkeypatch):
    repository = RunRepository(tmp_path / "runs")
    service = OptimizationService(repository)
    queued = OptimizationRun(request=small_request(), status=JobStatus.running)
    repository.save(queued)
    original_solve = service.solver.solve

    def solve_and_cancel(request, cancel_requested=None):
        del cancel_requested
        solved = original_solve(request)
        current = repository.get(queued.id)
        current.status = JobStatus.cancelled
        repository.save(current)
        return solved

    monkeypatch.setattr(service.solver, "solve", solve_and_cancel)
    service._solve_claimed(queued)
    assert repository.get(queued.id).status == JobStatus.cancelled
