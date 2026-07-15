from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .settings import settings


class Base(DeclarativeBase):
    pass


class SnapshotRow(Base):
    __tablename__ = "data_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    election_id: Mapped[str] = mapped_column(String(100), index=True)
    effective_date: Mapped[datetime] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="CREATED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    artifacts: Mapped[list["ArtifactRow"]] = relationship(back_populates="snapshot", cascade="all, delete-orphan")


class ArtifactRow(Base):
    __tablename__ = "source_artifacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("data_snapshots.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(20))
    url: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    snapshot: Mapped[SnapshotRow] = relationship(back_populates="artifacts")


class PrecinctRow(Base):
    __tablename__ = "precincts"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("data_snapshots.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    teryt: Mapped[str] = mapped_column(String(6), index=True)
    number: Mapped[int] = mapped_column(Integer)
    special: Mapped[bool] = mapped_column(default=False)
    population: Mapped[int | None] = mapped_column(Integer)
    eligible: Mapped[int] = mapped_column(Integer, default=0)
    votes: Mapped[dict] = mapped_column(JSON, default=dict)
    quality: Mapped[str] = mapped_column(String(20), default="none")
    reconstruction: Mapped[dict] = mapped_column(JSON, default=dict)
    # Atrybut pozostaje tekstowym EWKB dla przenośnych testów SQLite, ale jego
    # fizyczna nazwa musi odpowiadać kolumnie geometry(MultiPolygon,2180)
    # tworzonej przez migrację produkcyjną PostGIS.
    geometry_ewkb: Mapped[str | None] = mapped_column("geometry", Text)


class EdgeRow(Base):
    __tablename__ = "adjacency_edges"
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "source"],
            ["precincts.snapshot_id", "precincts.key"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "target"],
            ["precincts.snapshot_id", "precincts.key"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("snapshot_id", "source", "target", "kind"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("data_snapshots.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(100))
    shared_border_m: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(20), default="physical")


class OptimizationRow(Base):
    __tablename__ = "optimization_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    status: Mapped[str] = mapped_column(String(30), index=True)
    request: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    certificate_path: Mapped[str | None] = mapped_column(Text)
    certificate_verified: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def make_engine(url: str | None = None):
    return create_engine(url or settings.database_url)


def create_schema(url: str | None = None) -> None:
    Base.metadata.create_all(make_engine(url))


SessionLocal = sessionmaker(bind=make_engine(), expire_on_commit=False)
