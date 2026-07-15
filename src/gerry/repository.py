from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import UUID

from .domain import OptimizationRun


class RunRepository:
    """Atomic file repository usable locally and from a single Docker worker."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def save(self, run: OptimizationRun) -> None:
        run.updated_at = datetime.now(timezone.utc)
        path = self.root / f"{run.id}.json"
        temporary = path.with_suffix(".json.part")
        with self._lock:
            temporary.write_text(run.model_dump_json(indent=2), encoding="utf-8")
            temporary.replace(path)

    def get(self, run_id: UUID) -> OptimizationRun | None:
        path = self.root / f"{run_id}.json"
        if not path.exists():
            return None
        with self._lock:
            return OptimizationRun.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[OptimizationRun]:
        runs = [
            OptimizationRun.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.root.glob("*.json"))
        ]
        runs.sort(key=lambda run: (run.created_at, str(run.id)), reverse=True)
        return runs[offset:] if limit is None else runs[offset : offset + limit]

    def claim_next(self) -> OptimizationRun | None:
        """Claim one job for the single-process file backend."""
        with self._lock:
            queued = next(
                (
                    run
                    for run in reversed(self.list())
                    if run.status.value == "QUEUED"
                ),
                None,
            )
            if queued is None:
                return None
            from .domain import JobStatus

            queued.status = JobStatus.running
            queued.message = "Zadanie przejęte przez worker."
            self.save(queued)
            return queued

    def healthcheck(self) -> None:
        """Raise when the file backend cannot safely persist queue state."""
        probe = self.root / ".healthcheck"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()


class PostgresRunRepository:
    """Transactional queue shared safely by multiple API and worker processes."""

    def __init__(self, database_url: str):
        self.dsn = database_url.replace("postgresql+psycopg://", "postgresql://")

    @staticmethod
    def _json(run: OptimizationRun) -> dict:
        return run.model_dump(mode="json")

    def save(self, run: OptimizationRun) -> None:
        import psycopg
        from psycopg.types.json import Jsonb

        run.updated_at = datetime.now(timezone.utc)
        with psycopg.connect(self.dsn) as connection:
            connection.execute(
                """
                INSERT INTO optimization_runs
                    (id, status, request, result, certificate_path, certificate_verified,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    request = EXCLUDED.request,
                    result = EXCLUDED.result,
                    certificate_path = EXCLUDED.certificate_path,
                    certificate_verified = EXCLUDED.certificate_verified,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    run.id, run.status.value, Jsonb(run.request.model_dump(mode="json")),
                    Jsonb(self._json(run)), run.certificate_path, run.certificate_verified,
                    run.created_at, run.updated_at,
                ),
            )

    def get(self, run_id: UUID) -> OptimizationRun | None:
        import psycopg

        with psycopg.connect(self.dsn) as connection:
            row = connection.execute(
                "SELECT result FROM optimization_runs WHERE id = %s", (run_id,)
            ).fetchone()
        return OptimizationRun.model_validate(row[0]) if row else None

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[OptimizationRun]:
        import psycopg

        pagination = "" if limit is None else " LIMIT %s"
        parameters: tuple[int, ...] = (offset,) if limit is None else (limit, offset)
        with psycopg.connect(self.dsn) as connection:
            rows = connection.execute(
                "SELECT result FROM optimization_runs "
                "ORDER BY created_at DESC, id DESC"
                f"{pagination} OFFSET %s",
                parameters,
            ).fetchall()
        return [OptimizationRun.model_validate(row[0]) for row in rows]

    def claim_next(self) -> OptimizationRun | None:
        import psycopg
        from psycopg.types.json import Jsonb

        with psycopg.connect(self.dsn) as connection:
            row = connection.execute(
                """
                SELECT id, result
                FROM optimization_runs
                WHERE status = 'QUEUED'
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            run = OptimizationRun.model_validate(row[1])
            from .domain import JobStatus

            run.status = JobStatus.running
            run.message = "Zadanie przejęte transakcyjnie przez worker."
            run.updated_at = datetime.now(timezone.utc)
            connection.execute(
                """
                UPDATE optimization_runs
                SET status = 'RUNNING', result = %s, updated_at = %s
                WHERE id = %s
                """,
                (Jsonb(self._json(run)), run.updated_at, row[0]),
            )
            return run

    def healthcheck(self) -> None:
        import psycopg

        with psycopg.connect(self.dsn, connect_timeout=3) as connection:
            value = connection.execute("SELECT 1").fetchone()
        if value != (1,):
            raise RuntimeError("PostgreSQL nie odpowiedział na zapytanie kontrolne")
