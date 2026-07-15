from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GERRY_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/gerry.db"
    data_dir: Path = Path("data")
    law_snapshot: str = "2026-07-15"
    min_shared_border_m: float = 1.0
    inline_worker: bool = True

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    def ensure_dirs(self) -> None:
        for path in (self.raw_dir, self.processed_dir, self.artifacts_dir):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
