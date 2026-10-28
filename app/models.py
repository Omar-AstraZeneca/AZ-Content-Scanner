from datetime import datetime
from sqlalchemy import String, Integer, Float, DateTime, Date, Boolean, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ScanRun(Base):
    """Cada ejecución de escaneo. Base para las métricas del piloto."""
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market: Mapped[str] = mapped_column(String(10), index=True)
    site_url: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    site_country: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    site_category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # RUNNING | DONE | ERROR | CANCELLED
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    pages_scanned: Mapped[int] = mapped_column(Integer, default=0)
    pages_failed: Mapped[int] = mapped_column(Integer, default=0)
    assets_found: Mapped[int] = mapped_column(Integer, default=0)
    # Cobertura: cuántas imágenes se analizaron y cuántas no se pudieron leer
    images_analyzed: Mapped[int] = mapped_column(Integer, default=0)
    images_skipped: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    assets: Mapped[list["Asset"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    pages: Mapped[list["ScannedPage"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class ScannedPage(Base):
    """Registro de cada página visitada: permite auditar la cobertura de un escaneo."""
    __tablename__ = "scanned_pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("scan_runs.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    assets_found: Mapped[int] = mapped_column(Integer, default=0)
    images_analyzed: Mapped[int] = mapped_column(Integer, default=0)
    images_skipped: Mapped[int] = mapped_column(Integer, default=0)
    visited_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    run: Mapped["ScanRun"] = relationship(back_populates="pages")


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("scan_runs.id"), index=True)
    market: Mapped[str] = mapped_column(String(10), index=True)

    page_url: Mapped[str] = mapped_column(Text)
    asset_type: Mapped[str] = mapped_column(String(20), index=True)  # texto | imagen | video | pdf
    asset_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)

    veeva_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    expiry_year: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    expiry_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), index=True)  # VENCIDO | PROXIMO_A_VENCER | REVISAR | VIGENTE | SIN_DATO
    detection_method: Mapped[str] = mapped_column(String(30))  # regex | ocr | vlm | kaltura | veeva_api
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    veeva_detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON con detalle del ID
    evidence_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    scanned_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Cuántas páginas distintas muestran este mismo activo (1 = solo la de page_url)
    occurrences: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    run: Mapped["ScanRun"] = relationship(back_populates="assets")
    extra_occurrences: Mapped[list["AssetOccurrence"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )
    validation: Mapped["Validation | None"] = relationship(
        back_populates="asset", uselist=False, cascade="all, delete-orphan"
    )


class AssetOccurrence(Base):
    """Otras páginas donde aparece un activo ya registrado (la primera va en Asset.page_url).

    Un banner vencido puede estar en decenas de páginas: hay que saber DÓNDE retirarlo, no
    solo en cuál se vio primero."""
    __tablename__ = "asset_occurrences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    page_url: Mapped[str] = mapped_column(Text)
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    asset: Mapped["Asset"] = relationship(back_populates="extra_occurrences")


class Validation(Base):
    """Revisión manual (verdad de terreno) para medir la precisión del escáner."""
    __tablename__ = "validations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), unique=True, index=True)
    is_correct: Mapped[bool] = mapped_column(Boolean)  # ¿la detección fue correcta?
    corrected_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    corrected_veeva_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    asset: Mapped["Asset"] = relationship(back_populates="validation")


class ManualMissed(Base):
    """Activos que el escáner NO detectó pero la revisión manual sí (falsos negativos)."""
    __tablename__ = "manual_missed"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market: Mapped[str] = mapped_column(String(10), index=True)
    page_url: Mapped[str] = mapped_column(Text)
    asset_type: Mapped[str] = mapped_column(String(20))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)