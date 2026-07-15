from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

from .domain import JobStatus, OptimizationRequest, OptimizationRun
from .law import PROFILE_RULES
from .repository import PostgresRunRepository, RunRepository
from .settings import settings
from .solver import ExactEnumerator
from .scip_solver import ScipExactSolver


class OptimizationService:
    def __init__(self, repository: RunRepository | None = None):
        settings.ensure_dirs()
        self.repository = repository or (
            PostgresRunRepository(settings.database_url)
            if settings.database_url.startswith("postgresql")
            else RunRepository(settings.artifacts_dir / "runs")
        )
        self.solver = ExactEnumerator(settings.artifacts_dir / "certificates")
        self.scip_solver = ScipExactSolver(settings.artifacts_dir / "scip")
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gerry-solver")

    def submit(self, request: OptimizationRequest) -> OptimizationRun:
        self.validate_request(request)
        run = OptimizationRun(request=request)
        self.repository.save(run)
        if settings.inline_worker:
            self.executor.submit(self._solve, run.id)
        return run

    def process_queued(self) -> int:
        processed = 0
        while queued := self.repository.claim_next():
            self._solve_claimed(queued)
            processed += 1
        return processed

    def solve_now(self, request: OptimizationRequest) -> OptimizationRun:
        self.validate_request(request)
        if request.profile_id == "pl-europarlament@2026-07-15":
            run = OptimizationRun(
                request=request, status=JobStatus.objective_invariant,
                message="Łączna liczba mandatów komitetu w wyborach PE jest wyznaczana krajowo i nie zależy od granic okręgów.",
            )
        else:
            run = self.solver.solve(request) if len(request.nodes) <= 14 else self.scip_solver.solve(request)
        self.repository.save(run)
        return run

    @staticmethod
    def validate_request(request: OptimizationRequest) -> None:
        profile = PROFILE_RULES.get(request.profile_id)
        if profile is None:
            raise ValueError(f"Nieznany profil optymalizacji: {request.profile_id}")
        if request.target_kind not in profile["target_kinds"]:
            raise ValueError(
                f"Profil {request.profile_id} nie obsługuje celu {request.target_kind}"
            )

    def _solve(self, run_id: UUID) -> None:
        queued = self.repository.get(run_id)
        if queued is None or queued.status == JobStatus.cancelled:
            return
        queued.status = JobStatus.running
        queued.message = "Solver rozpoczął obliczenia."
        self.repository.save(queued)
        self._solve_claimed(queued)

    def _solve_claimed(self, queued: OptimizationRun) -> None:
        try:
            self.validate_request(queued.request)
            if queued.request.profile_id == "pl-europarlament@2026-07-15":
                solved = OptimizationRun(
                    request=queued.request, status=JobStatus.objective_invariant,
                    message="Łączna liczba mandatów komitetu w wyborach PE nie zależy od granic okręgów.",
                )
            else:
                solved = (
                    self.solver.solve(
                        queued.request, cancel_requested=lambda: self._is_cancelled(queued.id)
                    )
                    if len(queued.request.nodes) <= 14
                    else self.scip_solver.solve(
                        queued.request, cancel_requested=lambda: self._is_cancelled(queued.id)
                    )
                )
            solved.id = queued.id
            solved.created_at = queued.created_at
            current = self.repository.get(queued.id)
            if current is not None and current.status == JobStatus.cancelled:
                return
            self.repository.save(solved)
        except Exception as exc:  # worker boundary: persist failure instead of losing the job
            queued.status = JobStatus.failed
            queued.message = f"{type(exc).__name__}: {exc}"
            self.repository.save(queued)

    def _is_cancelled(self, run_id: UUID) -> bool:
        current = self.repository.get(run_id)
        return current is not None and current.status == JobStatus.cancelled

    def cancel(self, run_id: UUID) -> OptimizationRun | None:
        run = self.repository.get(run_id)
        if run and run.status in {JobStatus.queued, JobStatus.running, JobStatus.feasible_checkpoint}:
            run.status = JobStatus.cancelled
            run.message = "Anulowano przez użytkownika; bieżący proces zakończy się bez publikacji wyniku."
            self.repository.save(run)
        return run


optimization_service = OptimizationService()
