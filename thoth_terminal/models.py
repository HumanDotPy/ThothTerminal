from __future__ import annotations

from datetime import datetime, date
from copy import deepcopy
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    select,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, Session, attributes
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.dialects.postgresql import TSVECTOR


class Base(DeclarativeBase):
    pass


document_domains = Table(
    "document_domains",
    Base.metadata,
    Column("document_id", ForeignKey("documents.id"), primary_key=True),
    Column("domain_id", ForeignKey("domains.id"), primary_key=True),
)

document_tags = Table(
    "document_tags",
    Base.metadata,
    Column("document_id", ForeignKey("documents.id"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id"), primary_key=True),
)

document_event_types = Table(
    "document_event_types",
    Base.metadata,
    Column("document_id", ForeignKey("documents.id"), primary_key=True),
    Column("event_type_id", ForeignKey("event_types.id"), primary_key=True),
)


thesis_domains = Table(
    "thesis_domains",
    Base.metadata,
    Column("thesis_id", ForeignKey("theses.id"), primary_key=True),
    Column("domain_id", ForeignKey("domains.id"), primary_key=True),
)

thesis_tags = Table(
    "thesis_tags",
    Base.metadata,
    Column("thesis_id", ForeignKey("theses.id"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id"), primary_key=True),
)

thesis_event_types = Table(
    "thesis_event_types",
    Base.metadata,
    Column("thesis_id", ForeignKey("theses.id"), primary_key=True),
    Column("event_type_id", ForeignKey("event_types.id"), primary_key=True),
)

thesis_entities = Table(
    "thesis_entities",
    Base.metadata,
    Column("thesis_id", ForeignKey("theses.id"), primary_key=True),
    Column("entity_id", ForeignKey("entities.id"), primary_key=True),
)


note_domains = Table(
    "note_domains",
    Base.metadata,
    Column("note_id", ForeignKey("notes.id"), primary_key=True),
    Column("domain_id", ForeignKey("domains.id"), primary_key=True),
)

note_tags = Table(
    "note_tags",
    Base.metadata,
    Column("note_id", ForeignKey("notes.id"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id"), primary_key=True),
)

note_event_types = Table(
    "note_event_types",
    Base.metadata,
    Column("note_id", ForeignKey("notes.id"), primary_key=True),
    Column("event_type_id", ForeignKey("event_types.id"), primary_key=True),
)

note_entities = Table(
    "note_entities",
    Base.metadata,
    Column("note_id", ForeignKey("notes.id"), primary_key=True),
    Column("entity_id", ForeignKey("entities.id"), primary_key=True),
)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    polling_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    state: Mapped["SourceState"] = relationship(back_populates="source", uselist=False)
    profile: Mapped["SourceProfile | None"] = relationship(back_populates="source", uselist=False)


class SourceState(Base):
    __tablename__ = "source_states"

    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    source: Mapped[Source] = relationship(back_populates="state")


class SourceProfile(Base):
    __tablename__ = "source_profiles"

    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    source_family: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    rights_class: Mapped[str] = mapped_column(String(32), nullable=False, default="public")
    acquisition_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="connector")
    cadence_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    subject_binding_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    site_binding_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    jurisdiction_template_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vendor_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    source: Mapped["Source"] = relationship(back_populates="profile")


class SourceActor(Base):
    """Durable identity for a person, firm, publication, account, or data source."""

    __tablename__ = "source_actors"
    __table_args__ = (
        UniqueConstraint("actor_key", name="uq_source_actors_actor_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown", index=True)
    side: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    trust_label: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    aliases: Mapped[list["SourceActorAlias"]] = relationship(back_populates="actor")


class SourceActorAlias(Base):
    """Alias used to resolve source actors from X handles, names, domains, or watermarks."""

    __tablename__ = "source_actor_aliases"
    __table_args__ = (
        UniqueConstraint("alias_kind", "normalized_alias", "platform", name="uq_source_actor_aliases_lookup"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("source_actors.id"), nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    alias_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    platform: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    actor: Mapped[SourceActor] = relationship(back_populates="aliases")
    source_document: Mapped["Document | None"] = relationship()


class SourceAffiliation(Base):
    """Dated affiliation between a source person/account and a firm or organization."""

    __tablename__ = "source_affiliations"
    __table_args__ = (
        UniqueConstraint("affiliation_key", name="uq_source_affiliations_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    affiliation_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    person_actor_id: Mapped[int] = mapped_column(ForeignKey("source_actors.id"), nullable=False, index=True)
    organization_actor_id: Mapped[int] = mapped_column(ForeignKey("source_actors.id"), nullable=False, index=True)
    role_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role_class: Mapped[str] = mapped_column(String(64), nullable=False, default="analyst", index=True)
    side: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    evidence_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    person_actor: Mapped[SourceActor] = relationship(foreign_keys=[person_actor_id])
    organization_actor: Mapped[SourceActor] = relationship(foreign_keys=[organization_actor_id])
    source_document: Mapped["Document | None"] = relationship()


class FxRate(Base):
    __tablename__ = "fx_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base: Mapped[str] = mapped_column(String(8), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rates: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False, default=dict)


class Company(Base):
    """Legacy company model - kept for backward compatibility."""
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(64), nullable=True)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(128), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    supply_chain_stage: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ir_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    filings_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    reports_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    calendar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Link to new Issuer model for migration
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True)


# ==============================================================================
# THREE-RAIL ISSUER ARCHITECTURE
# Rail 1: Statutory Securities Filings (SEC EDGAR, SEDAR+, EDINET, OAMs)
# Rail 2: Exchange Regulated News (HKEX, NSE, LSE, etc.)
# Rail 3: Issuer IR Artifacts (press releases, presentations, webcasts, transcripts)
# ==============================================================================


class Issuer(Base):
    """
    Central entity for issuers, keyed by LEI where possible.
    Maps to securities via tickers/ISINs/CUSIPs/FIGIs.
    """
    __tablename__ = "issuers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    lei: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True, index=True)
    legal_name: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    jurisdiction: Mapped[str | None] = mapped_column(String(8), nullable=True)  # ISO 3166-1 alpha-2
    entity_status: Mapped[str | None] = mapped_column(String(32), nullable=True)  # ACTIVE, INACTIVE
    sector: Mapped[str | None] = mapped_column(String(128), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sic_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    website: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    ir_website: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # Relationships
    identifiers: Mapped[list["IssuerIdentifier"]] = relationship(back_populates="issuer")
    filings: Mapped[list["SecuritiesFiling"]] = relationship(back_populates="issuer")
    disclosures: Mapped[list["ExchangeDisclosure"]] = relationship(back_populates="issuer")
    ir_artifacts: Mapped[list["IRArtifact"]] = relationship(back_populates="issuer")
    earnings_events: Mapped[list["EarningsEvent"]] = relationship(back_populates="issuer")
    ipo_events: Mapped[list["IPOEvent"]] = relationship(back_populates="issuer")


class IssuerIdentifier(Base):
    """
    Maps various identifier types (ticker, ISIN, CUSIP, FIGI, CIK) to issuers.
    """
    __tablename__ = "issuer_identifiers"
    __table_args__ = (
        UniqueConstraint("id_type", "id_value", name="uq_issuer_identifier"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issuer_id: Mapped[int] = mapped_column(ForeignKey("issuers.id"), nullable=False)
    id_type: Mapped[str] = mapped_column(String(16), nullable=False)  # TICKER, ISIN, CUSIP, FIGI, CIK, SEDOL
    id_value: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    exchange: Mapped[str | None] = mapped_column(String(16), nullable=True)  # For tickers: NYSE, NASDAQ, TSE, etc.
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)  # ISO 4217
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    issuer: Mapped["Issuer"] = relationship(back_populates="identifiers")


class SecuritiesFiling(Base):
    """
    Rail 1: Statutory securities filings from regulators.
    Sources: SEC EDGAR, SEDAR+, EDINET, UK FCA NSM, ESMA OAMs, etc.
    """
    __tablename__ = "securities_filings"
    __table_args__ = (
        UniqueConstraint("regulator", "accession_number", name="uq_filing_accession"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issuer_id: Mapped[int] = mapped_column(ForeignKey("issuers.id"), nullable=False, index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)

    # Regulator info
    regulator: Mapped[str] = mapped_column(String(16), nullable=False)  # SEC, SEDAR, EDINET, FCA, etc.
    accession_number: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    filing_type: Mapped[str] = mapped_column(String(32), nullable=False)  # 10-K, 10-Q, 8-K, 6-K, DEF14A, etc.
    filing_subtype: Mapped[str | None] = mapped_column(String(64), nullable=True)  # Amendment, etc.

    # Dates
    filed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)  # Fiscal period end
    fiscal_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fiscal_quarter: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Content
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_document_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    index_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    filing_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # For 8-K: exhibit info
    exhibits: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # For XBRL filings
    has_xbrl: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    xbrl_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # Metadata
    is_earnings_release: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_guidance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw_meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    issuer: Mapped["Issuer"] = relationship(back_populates="filings")
    document: Mapped["Document"] = relationship()


class ExchangeDisclosure(Base):
    """
    Rail 2: Exchange-regulated announcements and disclosures.
    Sources: HKEX, NSE India, LSE RNS (via FCA NSM), JPX, etc.
    """
    __tablename__ = "exchange_disclosures"
    __table_args__ = (
        UniqueConstraint("exchange", "disclosure_id", name="uq_exchange_disclosure"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issuer_id: Mapped[int] = mapped_column(ForeignKey("issuers.id"), nullable=False, index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)

    # Exchange info
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)  # HKEX, NSE, LSE, TSE, KRX, TWSE, etc.
    disclosure_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    disclosure_type: Mapped[str] = mapped_column(String(64), nullable=False)  # Results, Material Event, etc.
    disclosure_subtype: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Dates
    announced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    fiscal_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fiscal_quarter: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Content
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    disclosure_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    attachment_urls: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # Classification
    is_earnings_related: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_material_event: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)

    raw_meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    issuer: Mapped["Issuer"] = relationship(back_populates="disclosures")
    document: Mapped["Document"] = relationship()


class IRArtifact(Base):
    """
    Rail 3: Issuer IR artifacts from company websites.
    Sources: IR websites, press release CDNs, webcast providers, etc.
    """
    __tablename__ = "ir_artifacts"
    __table_args__ = (
        UniqueConstraint("issuer_id", "artifact_type", "canonical_url", name="uq_ir_artifact"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issuer_id: Mapped[int] = mapped_column(ForeignKey("issuers.id"), nullable=False, index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)

    # Artifact type
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # press_release, presentation, transcript, webcast, audio, ical_event, fact_sheet, etc.
    artifact_subtype: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # earnings, guidance, investor_day, product_launch, etc.

    # Dates
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    fiscal_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fiscal_quarter: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Content
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    download_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    webcast_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    audio_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # For transcripts
    is_derived_transcript: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # True if we generated it via STT

    # ICS/Calendar info
    ical_uid: Mapped[str | None] = mapped_column(String(255), nullable=True)

    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    raw_meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    issuer: Mapped["Issuer"] = relationship(back_populates="ir_artifacts")
    document: Mapped["Document"] = relationship()


class EarningsEvent(Base):
    """
    Earnings calendar entries with provenance tracking.
    Aggregates info from filings, exchange announcements, and IR calendars.
    """
    __tablename__ = "earnings_events"
    __table_args__ = (
        UniqueConstraint("issuer_id", "fiscal_year", "fiscal_quarter", name="uq_earnings_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issuer_id: Mapped[int] = mapped_column(ForeignKey("issuers.id"), nullable=False, index=True)

    # Period
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    fiscal_quarter: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None for annual
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    calendar_quarter: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    issuer_fiscal_year_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    vendor_fiscal_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_label_raw: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Scheduled dates with provenance
    scheduled_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    scheduled_time: Mapped[str | None] = mapped_column(String(16), nullable=True)  # HH:MM in issuer TZ
    scheduled_tz: Mapped[str | None] = mapped_column(String(64), nullable=True)
    date_provenance: Mapped[str] = mapped_column(String(32), nullable=False, default="estimated")
    # issuer_published (highest), exchange_calendar, third_party_aggregated, estimated (lowest)
    date_source: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Actual dates (filled after event)
    actual_release_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    actual_call_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Status
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")
    # scheduled, confirmed, completed, postponed, cancelled

    # Links to artifacts
    release_filing_id: Mapped[int | None] = mapped_column(ForeignKey("securities_filings.id"), nullable=True)
    release_disclosure_id: Mapped[int | None] = mapped_column(ForeignKey("exchange_disclosures.id"), nullable=True)
    release_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("ir_artifacts.id"), nullable=True)
    presentation_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("ir_artifacts.id"), nullable=True)
    transcript_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("ir_artifacts.id"), nullable=True)
    webcast_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("ir_artifacts.id"), nullable=True)

    # For storing guidance if extracted
    guidance_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    raw_meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    issuer: Mapped["Issuer"] = relationship(back_populates="earnings_events")


class IPOEvent(Base):
    """Normalized IPO calendar event with source provenance and issuer/company links."""

    __tablename__ = "ipo_events"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_ipo_events_event_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True, index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    ingestion_run_id: Mapped[int | None] = mapped_column(ForeignKey("ingestion_runs.id"), nullable=True, index=True)

    region: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    market: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="filed", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    source_tier: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    company_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    ticker: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    isin: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    cik: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    security_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deal_type: Mapped[str | None] = mapped_column(String(64), nullable=True, default="ipo")

    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    pricing_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    offer_open_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    offer_close_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    application_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    approval_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    announcement_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    price_text: Mapped[str | None] = mapped_column(String(128), nullable=True)
    offer_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_range_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_range_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    shares_offered: Mapped[float | None] = mapped_column(Float, nullable=True)
    proceeds_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    proceeds_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    underwriters: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    issuer: Mapped["Issuer | None"] = relationship(back_populates="ipo_events")
    company: Mapped["Company | None"] = relationship()
    source: Mapped["Source | None"] = relationship()
    document: Mapped["Document | None"] = relationship()
    ingestion_run: Mapped["IngestionRun | None"] = relationship()


class InvestmentCatalyst(Base):
    """Investor-facing catalyst calendar entry with explicit provenance."""

    __tablename__ = "investment_catalysts"
    __table_args__ = (
        UniqueConstraint("catalyst_key", name="uq_investment_catalysts_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    catalyst_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    earnings_event_id: Mapped[int | None] = mapped_column(ForeignKey("earnings_events.id"), nullable=True, index=True)
    ipo_event_id: Mapped[int | None] = mapped_column(ForeignKey("ipo_events.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    catalyst_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="watch", index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_tier: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_kind: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject | None"] = relationship()
    issuer: Mapped["Issuer | None"] = relationship()
    company: Mapped["Company | None"] = relationship()
    source_document: Mapped["Document | None"] = relationship()
    earnings_event: Mapped["EarningsEvent | None"] = relationship()
    ipo_event: Mapped["IPOEvent | None"] = relationship()


class InvestmentMemo(Base):
    """Structured investment memo that agents and users can update."""

    __tablename__ = "investment_memos"
    __table_args__ = (
        UniqueConstraint("memo_key", name="uq_investment_memos_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    memo_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    rating: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    time_horizon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    thesis_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    valuation_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    catalysts_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    risks_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    subject: Mapped["ResearchSubject | None"] = relationship()
    issuer: Mapped["Issuer | None"] = relationship()
    company: Mapped["Company | None"] = relationship()


class AnalystEvidence(Base):
    """Durable analyst/media evidence such as ratings, price targets, estimates, and professional claims."""

    __tablename__ = "analyst_evidence"
    __table_args__ = (
        UniqueConstraint("evidence_key", name="uq_analyst_evidence_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    evidence_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True, index=True)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    evidence_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="analyst_note", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outlet: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    action: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    rating: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    prior_rating: Mapped[str | None] = mapped_column(String(64), nullable=True)
    price_target: Mapped[float | None] = mapped_column(Float, nullable=True)
    prior_price_target: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    target_horizon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    estimate_metric: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    estimate_period: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    estimate_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimate_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_tier: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_kind: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    trust_label: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    rights_class: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    observed_source_actor_id: Mapped[int | None] = mapped_column(ForeignKey("source_actors.id"), nullable=True, index=True)
    underlying_source_actor_id: Mapped[int | None] = mapped_column(ForeignKey("source_actors.id"), nullable=True, index=True)
    source_identity_status: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_side: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_role_class: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject | None"] = relationship()
    issuer: Mapped["Issuer | None"] = relationship()
    company: Mapped["Company | None"] = relationship()
    source: Mapped["Source | None"] = relationship()
    source_document: Mapped["Document | None"] = relationship()
    observed_source_actor: Mapped["SourceActor | None"] = relationship(foreign_keys=[observed_source_actor_id])
    underlying_source_actor: Mapped["SourceActor | None"] = relationship(foreign_keys=[underlying_source_actor_id])


class DecisionEvidenceLink(Base):
    """Generic source-to-decision provenance edge."""

    __tablename__ = "decision_evidence_links"
    __table_args__ = (
        UniqueConstraint(
            "target_type",
            "target_id",
            "evidence_object_type",
            "evidence_object_id",
            "relation",
            name="uq_decision_evidence_links_target_evidence",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    evidence_object_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    evidence_object_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    relation: Mapped[str] = mapped_column(String(64), nullable=False, default="supports", index=True)
    evidence_tier: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_kind: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject | None"] = relationship()
    issuer: Mapped["Issuer | None"] = relationship()
    company: Mapped["Company | None"] = relationship()


class SourceAttributionEdge(Base):
    """Normalized edge in a claim's source chain."""

    __tablename__ = "source_attribution_edges"
    __table_args__ = (
        UniqueConstraint("analyst_evidence_id", "edge_order", "role", name="uq_source_attribution_edges_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analyst_evidence_id: Mapped[int] = mapped_column(ForeignKey("analyst_evidence.id"), nullable=False, index=True)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    edge_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    role: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("source_actors.id"), nullable=True, index=True)
    raw_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_handle: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_firm: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extraction_method: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    analyst_evidence: Mapped[AnalystEvidence] = relationship()
    source_document: Mapped["Document | None"] = relationship()
    actor: Mapped["SourceActor | None"] = relationship()


class CompanyDocument(Base):
    __tablename__ = "company_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
    doc_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    company: Mapped[Company] = relationship()
    document: Mapped["Document"] = relationship()

    __table_args__ = (
        UniqueConstraint("company_id", "document_id", "doc_type", name="uq_company_document"),
    )


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    items_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    source: Mapped[Source] = relationship()


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    run_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Archive(Base):
    __tablename__ = "archives"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    object_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    language_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    translation_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_vector: Mapped[str | None] = mapped_column(
        Text().with_variant(TSVECTOR(), "postgresql"), nullable=True, deferred=True,
    )
    translation_backend: Mapped[str | None] = mapped_column(String(64), nullable=True)
    translation_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    translation_glossary_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    translated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    triage_state: Mapped[str] = mapped_column(String(16), nullable=False, default="NEW")
    saved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    saved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    actioned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ignored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    triage_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    storage_tier: Mapped[str] = mapped_column(String(16), nullable=False, default="hot")
    cold_archive_id: Mapped[int | None] = mapped_column(ForeignKey("archives.id"), nullable=True)

    source: Mapped[Source] = relationship()
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="document")
    passages: Mapped[list["DocumentPassage"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )
    pages: Mapped[list["DocumentPage"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )
    domains: Mapped[list["Domain"]] = relationship(secondary=document_domains)
    tags: Mapped[list["Tag"]] = relationship(secondary=document_tags)
    event_types: Mapped[list["EventType"]] = relationship(secondary=document_event_types)
    entity_links: Mapped[list["DocumentEntity"]] = relationship(back_populates="document")
    note_links: Mapped[list["NoteDocument"]] = relationship(back_populates="document")
    mentions_outgoing: Mapped[list["DocumentMention"]] = relationship(
        back_populates="source_document",
        foreign_keys="DocumentMention.source_document_id",
    )
    mentions_incoming: Mapped[list["DocumentMention"]] = relationship(
        back_populates="target_document",
        foreign_keys="DocumentMention.target_document_id",
    )


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    original_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    object_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    document: Mapped[Document] = relationship(back_populates="attachments")


class DocumentPassage(Base):
    __tablename__ = "document_passages"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "content_hash",
            "passage_index",
            name="uq_document_passages_content_index",
        ),
        Index("idx_document_passages_document_index", "document_id", "passage_index"),
        Index("idx_document_passages_content_hash", "document_id", "content_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    passage_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    passage_index: Mapped[int] = mapped_column(Integer, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heading: Mapped[str | None] = mapped_column(String(512), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    document: Mapped[Document] = relationship(back_populates="passages")


class DocumentPage(Base):
    __tablename__ = "document_pages"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "source_sha256",
            "page_number",
            name="uq_document_pages_source_page",
        ),
        Index("idx_document_pages_document_page", "document_id", "page_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    source_attachment_id: Mapped[int | None] = mapped_column(ForeignKey("attachments.id", ondelete="SET NULL"), nullable=True, index=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    document: Mapped[Document] = relationship(back_populates="pages")


class Domain(Base):
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class EventType(Base):
    __tablename__ = "event_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, default="other")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    aliases: Mapped[list["EntityAlias"]] = relationship(back_populates="entity")
    notes: Mapped[list["Note"]] = relationship(secondary=note_entities, back_populates="entities")


class EntityAlias(Base):
    __tablename__ = "entity_aliases"

    __table_args__ = (UniqueConstraint("entity_id", "alias", name="uq_entity_aliases_entity_alias"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False)
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)

    entity: Mapped[Entity] = relationship(back_populates="aliases")


class DocumentEntity(Base):
    __tablename__ = "document_entities"

    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), primary_key=True)
    method: Mapped[str] = mapped_column(String(32), nullable=False, default="dictionary")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    document: Mapped[Document] = relationship(back_populates="entity_links")
    entity: Mapped[Entity] = relationship()


class DocumentEvent(Base):
    __tablename__ = "document_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class DocumentMention(Base):
    __tablename__ = "document_mentions"
    __table_args__ = (
        UniqueConstraint("source_document_id", "target_document_id", "url", name="uq_document_mentions"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
    target_document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    source_document: Mapped["Document"] = relationship(
        back_populates="mentions_outgoing",
        foreign_keys=[source_document_id],
    )
    target_document: Mapped["Document"] = relationship(
        back_populates="mentions_incoming",
        foreign_keys=[target_document_id],
    )


class NotificationRule(Base):
    __tablename__ = "notification_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False, default="toast")
    source_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    domain_slugs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    entity_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    tag_slugs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    keyword_regex: Mapped[str | None] = mapped_column(String(512), nullable=True)
    quiet_start: Mapped[str | None] = mapped_column(String(8), nullable=True)
    quiet_end: Mapped[str | None] = mapped_column(String(8), nullable=True)
    quiet_tz: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    applied_at: Mapped[str] = mapped_column(Text, nullable=False)


class Thesis(Base):
    __tablename__ = "theses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_request_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="claim")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="watching")
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    stale_after_days: Mapped[int | None] = mapped_column(Integer, nullable=True, default=90)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_stale_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stale_check_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    domains: Mapped[list["Domain"]] = relationship(secondary=thesis_domains)
    tags: Mapped[list["Tag"]] = relationship(secondary=thesis_tags)
    event_types: Mapped[list["EventType"]] = relationship(secondary=thesis_event_types)
    entities: Mapped[list["Entity"]] = relationship(secondary=thesis_entities)
    evidence: Mapped[list["ThesisEvidence"]] = relationship(back_populates="thesis")
    proposition_links: Mapped[list["ThesisProposition"]] = relationship(back_populates="thesis")
    note_links: Mapped[list["NoteThesis"]] = relationship(back_populates="thesis")


class ThesisEvidence(Base):
    __tablename__ = "thesis_evidence"
    __table_args__ = (
        UniqueConstraint("thesis_id", "document_id", name="uq_thesis_evidence_thesis_document"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thesis_id: Mapped[int] = mapped_column(ForeignKey("theses.id"), nullable=False)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
    polarity: Mapped[str] = mapped_column(String(16), nullable=False, default="support")
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    weight_override: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    thesis: Mapped["Thesis"] = relationship(back_populates="evidence")
    document: Mapped["Document"] = relationship()


class Proposition(Base):
    __tablename__ = "propositions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # series | assertion | predicate | manual
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False, default="number")  # number | percent | bool | text
    period_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)  # flow | stock | rate (series only)
    rollup_rule: Mapped[str | None] = mapped_column(String(16), nullable=True)  # sum | avg | q4 | latest
    stale_after_days_default: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    observations: Mapped[list["Observation"]] = relationship(back_populates="proposition")
    thesis_links: Mapped[list["ThesisProposition"]] = relationship(back_populates="proposition")
    note_links: Mapped[list["NoteProposition"]] = relationship(back_populates="proposition")


class ThesisProposition(Base):
    __tablename__ = "thesis_propositions"

    thesis_id: Mapped[int] = mapped_column(ForeignKey("theses.id"), primary_key=True)
    proposition_id: Mapped[int] = mapped_column(ForeignKey("propositions.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="key")  # key | supporting | risk | watch
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    thesis: Mapped["Thesis"] = relationship(back_populates="proposition_links")
    proposition: Mapped["Proposition"] = relationship(back_populates="thesis_links")


class Observation(Base):
    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposition_id: Mapped[int] = mapped_column(ForeignKey("propositions.id"), nullable=False)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)

    value_number: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_bool: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    target_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_quarter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    period_granularity: Mapped[str | None] = mapped_column(String(16), nullable=True)  # year | quarter
    estimate_flag: Mapped[str | None] = mapped_column(String(8), nullable=True)  # A | E | F
    as_of_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    proposition: Mapped["Proposition"] = relationship(back_populates="observations")
    document: Mapped["Document"] = relationship()


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_request_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    note_type: Mapped[str] = mapped_column(String(32), nullable=False, default="general")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active | archived
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stale_after_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_stale_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stale_check_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    domains: Mapped[list["Domain"]] = relationship(secondary=note_domains)
    tags: Mapped[list["Tag"]] = relationship(secondary=note_tags)
    event_types: Mapped[list["EventType"]] = relationship(secondary=note_event_types)
    entities: Mapped[list["Entity"]] = relationship(secondary=note_entities, back_populates="notes")

    thesis_links: Mapped[list["NoteThesis"]] = relationship(back_populates="note")
    document_links: Mapped[list["NoteDocument"]] = relationship(back_populates="note")
    proposition_links: Mapped[list["NoteProposition"]] = relationship(back_populates="note")


class NoteThesis(Base):
    __tablename__ = "note_theses"
    __table_args__ = (
        UniqueConstraint("note_id", "thesis_id", name="uq_note_theses_note_thesis"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"), nullable=False)
    thesis_id: Mapped[int] = mapped_column(ForeignKey("theses.id"), nullable=False)
    relation: Mapped[str] = mapped_column(String(16), nullable=False, default="context")  # context | assumption | impact | counterpoint | reference
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    note: Mapped["Note"] = relationship(back_populates="thesis_links")
    thesis: Mapped["Thesis"] = relationship(back_populates="note_links")


class NoteDocument(Base):
    __tablename__ = "note_documents"
    __table_args__ = (
        UniqueConstraint("note_id", "document_id", name="uq_note_documents_note_document"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"), nullable=False)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    note: Mapped["Note"] = relationship(back_populates="document_links")
    document: Mapped["Document"] = relationship(back_populates="note_links")


class NoteProposition(Base):
    __tablename__ = "note_propositions"
    __table_args__ = (
        UniqueConstraint("note_id", "proposition_id", name="uq_note_propositions_note_prop"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"), nullable=False)
    proposition_id: Mapped[int] = mapped_column(ForeignKey("propositions.id"), nullable=False)
    relation: Mapped[str] = mapped_column(String(16), nullable=False, default="references")  # references | assumption | derived_from
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    note: Mapped["Note"] = relationship(back_populates="proposition_links")
    proposition: Mapped["Proposition"] = relationship(back_populates="note_links")


class AgentAction(Base):
    __tablename__ = "agent_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    thesis_id: Mapped[int | None] = mapped_column(ForeignKey("theses.id"), nullable=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    note_id: Mapped[int | None] = mapped_column(ForeignKey("notes.id"), nullable=True)
    source_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    thesis: Mapped["Thesis | None"] = relationship()
    document: Mapped["Document | None"] = relationship()
    note: Mapped["Note | None"] = relationship()
    source_job: Mapped["Job | None"] = relationship()


class AgentChangeRun(Base):
    __tablename__ = "agent_change_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    task_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    engine_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="legacy_internal")
    automation_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note_id: Mapped[int | None] = mapped_column(ForeignKey("notes.id"), nullable=True)
    source_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    note: Mapped["Note | None"] = relationship()
    source_job: Mapped["Job | None"] = relationship()
    changes: Mapped[list["AgentChangeEvent"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class AgentChangeEvent(Base):
    __tablename__ = "agent_change_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_change_runs.id"), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    change_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    before_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    run: Mapped["AgentChangeRun"] = relationship(back_populates="changes")


class CloudAgentMemorySegment(Base):
    __tablename__ = "cloud_agent_memory_segments"
    __table_args__ = (
        UniqueConstraint("agent_key", "segment_key", name="uq_cloud_agent_memory_segments_agent_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    segment_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    segment_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cache_scope: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50, index=True)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entity_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    theme_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_run_id: Mapped[int | None] = mapped_column(ForeignKey("cloud_goal_runs.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    compressed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    source_run: Mapped["CloudGoalRun | None"] = relationship()
    events: Mapped[list["CloudAgentMemoryEvent"]] = relationship(back_populates="segment")


class CloudAgentMemoryEvent(Base):
    __tablename__ = "cloud_agent_memory_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    segment_id: Mapped[int | None] = mapped_column(ForeignKey("cloud_agent_memory_segments.id"), nullable=True, index=True)
    source_run_id: Mapped[int | None] = mapped_column(ForeignKey("cloud_goal_runs.id"), nullable=True, index=True)
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    segment: Mapped["CloudAgentMemorySegment | None"] = relationship(back_populates="events")
    source_run: Mapped["CloudGoalRun | None"] = relationship()


class CloudPromptStateSnapshot(Base):
    """Immutable compiled state prefix shared by cloud research calls for an epoch."""

    __tablename__ = "cloud_prompt_state_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_key", name="uq_cloud_prompt_state_snapshot_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    agent_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    compiler_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    epoch_start: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    epoch_end: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CloudGoalRun(Base):
    __tablename__ = "cloud_goal_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running", index=True)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="deepseek")
    model: Mapped[str] = mapped_column(String(255), nullable=False, default="deepseek-v4-flash")
    routed_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_context_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    final_result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    token_policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    events: Mapped[list["CloudGoalEvent"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    projections: Mapped[list["CloudGoalProjection"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class CloudGoalEvent(Base):
    __tablename__ = "cloud_goal_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("cloud_goal_runs.id"), nullable=False, index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    run: Mapped["CloudGoalRun"] = relationship(back_populates="events")


class CloudGoalProjection(Base):
    """Immutable derived working state with replayable event lineage."""

    __tablename__ = "cloud_goal_projections"
    __table_args__ = (
        UniqueConstraint("projection_key", name="uq_cloud_goal_projection_key"),
        UniqueConstraint("run_id", "sequence", name="uq_cloud_goal_projection_sequence"),
        Index("ix_cloud_goal_projection_active", "run_id", "status", "sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    projection_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("cloud_goal_runs.id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    projection_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    previous_projection_id: Mapped[int | None] = mapped_column(
        ForeignKey("cloud_goal_projections.id"), nullable=True, index=True
    )
    source_event_from_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_event_to_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_event_ids_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    source_events_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    structured_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    request_manifest_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_request_manifests.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    run: Mapped["CloudGoalRun"] = relationship(back_populates="projections")


class CloudGoalLineage(Base):
    """Durable parent/child delegation and accepted-output lineage."""

    __tablename__ = "cloud_goal_lineages"
    __table_args__ = (
        UniqueConstraint("lineage_key", name="uq_cloud_goal_lineage_key"),
        UniqueConstraint("parent_run_id", "step_id", name="uq_cloud_goal_lineage_parent_step"),
        UniqueConstraint("child_run_id", name="uq_cloud_goal_lineage_child"),
        Index("ix_cloud_goal_lineage_parent_status", "parent_run_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lineage_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    parent_run_id: Mapped[int] = mapped_column(ForeignKey("cloud_goal_runs.id"), nullable=False, index=True)
    child_run_id: Mapped[int] = mapped_column(ForeignKey("cloud_goal_runs.id"), nullable=False, index=True)
    step_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="delegated", index=True)
    terminal_disposition: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    delegation_intent_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    capability_profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    permission_profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    model_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    delegating_request_manifest_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_request_manifests.id"), nullable=True, index=True
    )
    accepted_request_manifest_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_request_manifests.id"), nullable=True, index=True
    )
    accepted_output_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("cloud_goal_events.id"), nullable=True, index=True
    )
    accepted_output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    accepted_output_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentRequestManifest(Base):
    """Immutable, model-visible request envelope persisted before dispatch."""

    __tablename__ = "agent_request_manifests"
    __table_args__ = (
        UniqueConstraint("request_key", name="uq_agent_request_manifest_key"),
        Index("ix_agent_request_manifest_run", "run_kind", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    task_kind: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    requested_model: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    system_prompt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    system_prompt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    messages_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    context_sections_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    tool_names_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    tool_schema_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    permission_profile: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_refs_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    request_parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    prompt_cache_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    prompt_cache_retention: Mapped[str | None] = mapped_column(String(32), nullable=True)
    application_version: Mapped[str] = mapped_column(String(64), nullable=False)
    git_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    runtime_boot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    outcome: Mapped["AgentRequestOutcome | None"] = relationship(
        back_populates="manifest", cascade="all, delete-orphan", uselist=False
    )


class AgentRequestOutcome(Base):
    """Append-only terminal observation linked to an immutable request manifest."""

    __tablename__ = "agent_request_outcomes"
    __table_args__ = (
        UniqueConstraint("manifest_id", name="uq_agent_request_outcome_manifest"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    manifest_id: Mapped[int] = mapped_column(
        ForeignKey("agent_request_manifests.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    response_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    response_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    response_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    manifest: Mapped["AgentRequestManifest"] = relationship(back_populates="outcome")


class AgentInboxItem(Base):
    """Durable agent input whose model-visibility and wake semantics are explicit."""

    __tablename__ = "agent_inbox_items"
    __table_args__ = (
        UniqueConstraint("target_kind", "target_id", "dedupe_key", name="uq_agent_inbox_target_dedupe"),
        Index("ix_agent_inbox_claim", "target_kind", "target_id", "status", "delivery_mode", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    delivery_mode: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_kind: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    wake_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NewsletterSubscriber(Base):
    __tablename__ = "newsletter_subscribers"
    __table_args__ = (
        UniqueConstraint("email_normalized", name="uq_newsletter_subscribers_email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="public_site")
    consent_version: Mapped[str] = mapped_column(String(32), nullable=False, default="2026-08-03")
    consented_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_confirmation_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NewsletterEdition(Base):
    __tablename__ = "newsletter_editions"
    __table_args__ = (
        UniqueConstraint("source_run_id", name="uq_newsletter_editions_source_run"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_run_id: Mapped[int] = mapped_column(
        ForeignKey("cloud_goal_runs.id"), nullable=False, index=True
    )
    subject: Mapped[str] = mapped_column(String(240), nullable=False)
    preview_text: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NewsletterDelivery(Base):
    __tablename__ = "newsletter_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "edition_id",
            "subscriber_id",
            name="uq_newsletter_deliveries_edition_subscriber",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    edition_id: Mapped[int] = mapped_column(
        ForeignKey("newsletter_editions.id"), nullable=False, index=True
    )
    subscriber_id: Mapped[int] = mapped_column(
        ForeignKey("newsletter_subscribers.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class LocalAgentRun(Base):
    __tablename__ = "local_agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running", index=True)
    model_profile: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class LocalAgentAction(Base):
    __tablename__ = "local_agent_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("local_agent_runs.id"), nullable=True, index=True)
    action_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="success", index=True)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    endpoint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    destination_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    destination_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    run: Mapped["LocalAgentRun | None"] = relationship()
    document: Mapped["Document | None"] = relationship()


class InteractiveAgentOperation(Base):
    __tablename__ = "interactive_agent_operations"
    __table_args__ = (UniqueConstraint("operation_key", name="uq_interactive_agent_operation_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ExternalAgentPrincipal(Base):
    """An invited user whose agent API credentials can be revoked independently."""

    __tablename__ = "external_agent_principals"
    __table_args__ = (UniqueConstraint("slug", name="uq_external_agent_principal_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ExternalAgentApiKey(Base):
    __tablename__ = "external_agent_api_keys"
    __table_args__ = (UniqueConstraint("key_prefix", name="uq_external_agent_api_key_prefix"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    principal_id: Mapped[int] = mapped_column(ForeignKey("external_agent_principals.id"), nullable=False, index=True)
    key_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    principal: Mapped[ExternalAgentPrincipal] = relationship()


class ExternalAgentUsageDay(Base):
    __tablename__ = "external_agent_usage_days"
    __table_args__ = (UniqueConstraint("principal_id", "usage_date", name="uq_external_agent_usage_principal_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    principal_id: Mapped[int] = mapped_column(ForeignKey("external_agent_principals.id"), nullable=False, index=True)
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    upload_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    render_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    proposal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ExternalModelProjection(Base):
    """A deliberately released, bounded model view; never a raw model serializer."""

    __tablename__ = "external_model_projections"
    __table_args__ = (UniqueConstraint("model_key", "version", name="uq_external_model_projection_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_type: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_document_ids_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="published")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ExternalModelWithdrawal(Base):
    __tablename__ = "external_model_withdrawals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    withdrawn_versions_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ExternalAgentEvidence(Base):
    __tablename__ = "external_agent_evidence"
    __table_args__ = (UniqueConstraint("principal_id", "document_id", name="uq_external_agent_evidence_owner"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    principal_id: Mapped[int] = mapped_column(ForeignKey("external_agent_principals.id"), nullable=False, index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ExternalAgentResearchRequest(Base):
    __tablename__ = "external_agent_research_requests"
    __table_args__ = (UniqueConstraint("principal_id", "request_key", name="uq_external_agent_research_request_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    principal_id: Mapped[int] = mapped_column(ForeignKey("external_agent_principals.id"), nullable=False, index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    request_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="submitted")
    operator_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_operation_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExternalAgentProposal(Base):
    __tablename__ = "external_agent_proposals"
    __table_args__ = (UniqueConstraint("principal_id", "request_key", name="uq_external_agent_proposal_request"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    principal_id: Mapped[int] = mapped_column(ForeignKey("external_agent_principals.id"), nullable=False, index=True)
    request_key: Mapped[str] = mapped_column(String(120), nullable=False)
    target_model_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    document_ids_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="submitted")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    operator_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_operation_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LocalAgentDocumentRead(Base):
    __tablename__ = "local_agent_document_reads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("local_agent_runs.id"), nullable=True, index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    read_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="full")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    run: Mapped["LocalAgentRun | None"] = relationship()
    document: Mapped["Document"] = relationship()


class LocalAgentDocumentStageState(Base):
    __tablename__ = "local_agent_document_stage_states"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_local_agent_document_stage_state_doc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    window_kind: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    window_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    window_ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    prep_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    ocr_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    vlm_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    translation_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    signal_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    durable_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    media_pending_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    media_done_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    non_signal_media_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quarantine_stage: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    terminal_skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_stage: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    model_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    document: Mapped["Document"] = relationship()


class LocalAgentDocumentCompression(Base):
    __tablename__ = "local_agent_document_compressions"
    __table_args__ = (
        UniqueConstraint("document_id", "prompt_version", "model", name="uq_local_agent_compression_doc_prompt_model"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("local_agent_runs.id"), nullable=True, index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    summary_short: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    atomic_claims_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    entity_refs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    supply_chain_refs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    technology_refs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    unresolved_entities_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    information_gain: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    document_disposition: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="compressed", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    run: Mapped["LocalAgentRun | None"] = relationship()
    document: Mapped["Document"] = relationship()


class LocalAgentDeepAnalysisTrace(Base):
    __tablename__ = "local_agent_deep_analysis_traces"
    __table_args__ = (
        UniqueConstraint("compression_id", "prompt_version", "model", name="uq_local_agent_deep_trace_compression_prompt_model"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("local_agent_runs.id"), nullable=True, index=True)
    compression_id: Mapped[int] = mapped_column(ForeignKey("local_agent_document_compressions.id"), nullable=False, index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    analysis_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="idle_deep", index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="success", index=True)
    signal_level: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    signal_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    key_findings_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    knowledge_connections_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    contradictions_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    open_questions_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    followups_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    run: Mapped["LocalAgentRun | None"] = relationship()
    compression: Mapped["LocalAgentDocumentCompression"] = relationship()
    document: Mapped["Document"] = relationship()


class LocalAgentEntityClaim(Base):
    __tablename__ = "local_agent_entity_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("local_agent_runs.id"), nullable=True, index=True)
    compression_id: Mapped[int | None] = mapped_column(ForeignKey("local_agent_document_compressions.id"), nullable=True, index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    entity_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    destination_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    destination_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    claim_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="business_update", index=True)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    routing_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    run: Mapped["LocalAgentRun | None"] = relationship()
    compression: Mapped["LocalAgentDocumentCompression | None"] = relationship()
    document: Mapped["Document | None"] = relationship()


class LocalAgentEntityRoute(Base):
    __tablename__ = "local_agent_entity_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("local_agent_runs.id"), nullable=True, index=True)
    compression_id: Mapped[int | None] = mapped_column(ForeignKey("local_agent_document_compressions.id"), nullable=True, index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    lane: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    entity_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown", index=True)
    entity_role: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    summary_relevance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    summary_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    durable_relevance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    durable_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    durable_action: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    materiality: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    novelty: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    temporal_posture: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    evidence_tier: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    resolution_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    new_information: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_claim_indexes_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    destination_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    destination_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    run: Mapped["LocalAgentRun | None"] = relationship()
    compression: Mapped["LocalAgentDocumentCompression | None"] = relationship()
    document: Mapped["Document | None"] = relationship()


class LocalAgentWriteDiff(Base):
    __tablename__ = "local_agent_write_diffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("local_agent_runs.id"), nullable=True, index=True)
    action_id: Mapped[int | None] = mapped_column(ForeignKey("local_agent_actions.id"), nullable=True, index=True)
    destination_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    destination_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_document_ids_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    before_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    after_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    write_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    run: Mapped["LocalAgentRun | None"] = relationship()
    action: Mapped["LocalAgentAction | None"] = relationship()


class LocalAgentUnresolvedEntity(Base):
    __tablename__ = "local_agent_unresolved_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("local_agent_runs.id"), nullable=True, index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    run: Mapped["LocalAgentRun | None"] = relationship()
    document: Mapped["Document | None"] = relationship()


class LocalAgentTranslationOverride(Base):
    __tablename__ = "local_agent_translation_overrides"
    __table_args__ = (
        UniqueConstraint("source_text", "canonical_text", "language", name="uq_local_agent_translation_override_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("local_agent_runs.id"), nullable=True, index=True)
    source_text: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    canonical_text: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    aliases_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_seen_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    run: Mapped["LocalAgentRun | None"] = relationship()
    first_seen_document: Mapped["Document | None"] = relationship()


class LocalAgentDailySummary(Base):
    __tablename__ = "local_agent_daily_summaries"
    __table_args__ = (
        UniqueConstraint("summary_date", "summary_kind", name="uq_local_agent_daily_summary_date_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("local_agent_runs.id"), nullable=True, index=True)
    summary_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    summary_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="global")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_document_ids_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    compression_ids_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    run: Mapped["LocalAgentRun | None"] = relationship()


class LocalAgentReviewDecision(Base):
    __tablename__ = "local_agent_review_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class LocalAgentEvalCase(Base):
    __tablename__ = "local_agent_eval_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_label: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    fixture_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    fixture_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    tags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    document: Mapped["Document | None"] = relationship()
    runs: Mapped[list["LocalAgentEvalRun"]] = relationship(back_populates="case")


class LocalAgentEvalRun(Base):
    __tablename__ = "local_agent_eval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("local_agent_eval_cases.id"), nullable=False, index=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="success", index=True)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    score_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    case: Mapped["LocalAgentEvalCase"] = relationship(back_populates="runs")


class ThesisProposal(Base):
    __tablename__ = "thesis_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="claim")
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    stale_after_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    novelty_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    note_id: Mapped[int | None] = mapped_column(ForeignKey("notes.id"), nullable=True)
    source_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    approved_thesis_id: Mapped[int | None] = mapped_column(ForeignKey("theses.id"), nullable=True)
    client_request_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    note: Mapped["Note | None"] = relationship()
    source_job: Mapped["Job | None"] = relationship(foreign_keys=[source_job_id])
    approved_thesis: Mapped["Thesis | None"] = relationship(foreign_keys=[approved_thesis_id])


# ---------------------------------------------------------------------------
# Visualization Data Models (ChipBook-style charts)
# ---------------------------------------------------------------------------

class DataSeries(Base):
    """
    A named time-series for visualization (e.g., Taiwan DRAM Inventory, WFE Imports).
    Supports both actual data and estimates/forecasts.
    """
    __tablename__ = "data_series"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_data_series_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)  # e.g., "taiwan-dram-inventory-volume"
    name: Mapped[str] = mapped_column(String(255), nullable=False)  # Display name
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="general")  # memory, wfe, smartphones, etc.
    unit: Mapped[str] = mapped_column(String(32), nullable=False, default="units")  # units, M Pcs., M$, %, WPM, etc.
    frequency: Mapped[str] = mapped_column(String(16), nullable=False, default="monthly")  # daily, weekly, monthly, quarterly, annual
    source_label: Mapped[str | None] = mapped_column(String(128), nullable=True)  # e.g., "TSIA", "Korea Customs", "Semi.org"
    axis_type: Mapped[str] = mapped_column(String(16), nullable=False, default="value")  # value, log, percent
    chart_type: Mapped[str] = mapped_column(String(16), nullable=False, default="line")  # line, bar, area
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)  # hex color for series
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    points: Mapped[list["DataPoint"]] = relationship(back_populates="series", cascade="all, delete-orphan")
    chart_series: Mapped[list["ChartSeries"]] = relationship(back_populates="data_series")


class DataPoint(Base):
    """
    A single data point in a time series.
    Supports actuals vs estimates via is_estimate flag.
    """
    __tablename__ = "data_points"
    __table_args__ = (
        UniqueConstraint("series_id", "period_date", "is_estimate", name="uq_data_points_series_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("data_series.id"), nullable=False, index=True)
    period_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)  # First day of period (month, quarter, etc.)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    is_estimate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # True for forecasts/estimates
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # Track revisions
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)  # URL or reference
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    series: Mapped["DataSeries"] = relationship(back_populates="points")


class Chart(Base):
    """
    A visualization chart that can combine multiple data series.
    Can be linked to Theses or Notes for context.
    """
    __tablename__ = "charts"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_charts_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    subtitle: Mapped[str | None] = mapped_column(String(512), nullable=True)
    chart_style: Mapped[str] = mapped_column(String(32), nullable=False, default="combo")  # combo, stacked_bar, line, area
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    description_md: Mapped[str | None] = mapped_column(Text, nullable=True)  # Monthly updates, context
    related_equities: Mapped[str | None] = mapped_column(String(512), nullable=True)  # Comma-separated tickers
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)  # ECharts options override

    series_links: Mapped[list["ChartSeries"]] = relationship(back_populates="chart", cascade="all, delete-orphan")
    dataset_view_links: Mapped[list["ChartDatasetView"]] = relationship(
        back_populates="chart",
        cascade="all, delete-orphan",
    )
    thesis_id: Mapped[int | None] = mapped_column(ForeignKey("theses.id"), nullable=True)
    note_id: Mapped[int | None] = mapped_column(ForeignKey("notes.id"), nullable=True)


class ChartSeries(Base):
    """
    Links a DataSeries to a Chart with display configuration.
    """
    __tablename__ = "chart_series"
    __table_args__ = (
        UniqueConstraint("chart_id", "series_id", name="uq_chart_series_chart_series"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chart_id: Mapped[int] = mapped_column(ForeignKey("charts.id"), nullable=False, index=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("data_series.id"), nullable=False, index=True)
    y_axis_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0 = left axis, 1 = right axis
    display_type: Mapped[str] = mapped_column(String(16), nullable=False, default="line")  # line, bar, area (override)
    stack_group: Mapped[str | None] = mapped_column(String(32), nullable=True)  # For stacked bar charts
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)  # Override color
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    chart: Mapped["Chart"] = relationship(back_populates="series_links")
    data_series: Mapped["DataSeries"] = relationship(back_populates="chart_series")


class Dataset(Base):
    """Versioned tabular dataset used for analyst workbenches and agent tooling."""

    __tablename__ = "datasets"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_datasets_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    family: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    source_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    versions: Mapped[list["DatasetVersion"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")
    managed_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    canonical_dataset_id: Mapped[int | None] = mapped_column(ForeignKey("datasets.id"), nullable=True, index=True)
    views: Mapped[list["DatasetView"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")
    links: Mapped[list["DatasetLink"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")
    metric_bindings: Mapped[list["SupplyChainMetricBinding"]] = relationship(back_populates="dataset")


class DatasetVersion(Base):
    """Immutable or draft snapshot of a dataset schema and its rows."""

    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("dataset_id", "version", "status", name="uq_dataset_versions_dataset_version_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # draft | published
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_event: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    dataset: Mapped["Dataset"] = relationship(back_populates="versions")
    columns: Mapped[list["DatasetColumn"]] = relationship(back_populates="version", cascade="all, delete-orphan")
    rows: Mapped[list["DatasetRow"]] = relationship(back_populates="version", cascade="all, delete-orphan")


class DatasetColumn(Base):
    __tablename__ = "dataset_columns"
    __table_args__ = (
        UniqueConstraint("dataset_version_id", "key", name="uq_dataset_columns_version_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_versions.id"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    data_type: Mapped[str] = mapped_column(String(24), nullable=False, default="text")  # text | number | date | bool | json
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)  # dimension | metric | timestamp | id
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    version: Mapped["DatasetVersion"] = relationship(back_populates="columns")


class DatasetRowContent(Base):
    """One immutable payload, shared by versioned row memberships."""

    __tablename__ = "dataset_row_contents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    values: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class DatasetRow(Base):
    __tablename__ = "dataset_rows"
    __table_args__ = (
        UniqueConstraint("dataset_version_id", "row_key", name="uq_dataset_rows_version_row_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_versions.id"), nullable=False, index=True)
    row_key: Mapped[str] = mapped_column(String(128), nullable=False)
    content_id: Mapped[int] = mapped_column(ForeignKey("dataset_row_contents.id"), nullable=False, index=True)
    content: Mapped["DatasetRowContent"] = relationship(lazy="joined")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    version: Mapped["DatasetVersion"] = relationship(back_populates="rows")

    @hybrid_property
    def values(self) -> dict[str, Any]:
        pending = self.__dict__.get("_pending_dataset_values")
        if pending is not None:
            return deepcopy(pending)
        return deepcopy(self.content.values) if self.content is not None else {}

    @values.setter
    def values(self, value: dict[str, Any]) -> None:
        self.__dict__["_pending_dataset_values"] = deepcopy(value or {})
        state = attributes.instance_state(self)
        if state.persistent:
            attributes.flag_dirty(self)

    @values.expression
    def values(cls):
        return (
            select(DatasetRowContent.values)
            .where(DatasetRowContent.id == cls.content_id)
            .correlate(cls)
            .scalar_subquery()
        )


@event.listens_for(Session, "before_flush")
def _prepare_dataset_row_payloads(session, _context, _instances):
    from thoth_terminal.dataset_storage import prepare_dataset_row_payloads

    prepare_dataset_row_payloads(session)


class DatasetView(Base):
    __tablename__ = "dataset_views"
    __table_args__ = (
        UniqueConstraint("dataset_id", "slug", name="uq_dataset_views_dataset_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    filters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    columns: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    sort_spec: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    dataset: Mapped["Dataset"] = relationship(back_populates="views")
    chart_links: Mapped[list["ChartDatasetView"]] = relationship(
        back_populates="dataset_view",
        cascade="all, delete-orphan",
    )
    metric_bindings: Mapped[list["SupplyChainMetricBinding"]] = relationship(back_populates="dataset_view")


class DatasetLink(Base):
    __tablename__ = "dataset_links"
    __table_args__ = (
        UniqueConstraint("dataset_id", "object_type", "object_id", "relation", name="uq_dataset_links_target"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)  # thesis | note | supply_chain
    object_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    relation: Mapped[str] = mapped_column(String(32), nullable=False, default="context")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    dataset: Mapped["Dataset"] = relationship(back_populates="links")


class ChartDatasetView(Base):
    """Bind a chart to a dataset view for dataset-backed chart surfaces."""

    __tablename__ = "chart_dataset_views"
    __table_args__ = (
        UniqueConstraint("chart_id", "dataset_view_id", "y_key", name="uq_chart_dataset_view_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chart_id: Mapped[int] = mapped_column(ForeignKey("charts.id"), nullable=False, index=True)
    dataset_view_id: Mapped[int] = mapped_column(ForeignKey("dataset_views.id"), nullable=False, index=True)
    x_key: Mapped[str] = mapped_column(String(128), nullable=False)
    y_key: Mapped[str] = mapped_column(String(128), nullable=False)
    series_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    y_axis_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    display_type: Mapped[str] = mapped_column(String(16), nullable=False, default="line")
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    chart: Mapped["Chart"] = relationship(back_populates="dataset_view_links")
    dataset_view: Mapped["DatasetView"] = relationship(back_populates="chart_links")


class StructuredDataFact(Base):
    """Normalized append-safe fact rows that materialize into curated datasets."""

    __tablename__ = "structured_data_facts"
    __table_args__ = (
        UniqueConstraint("dataset_family", "fact_key", name="uq_structured_data_facts_family_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_family: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    fact_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    group_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True, index=True)
    ingestion_run_id: Mapped[int | None] = mapped_column(ForeignKey("ingestion_runs.id"), nullable=True, index=True)
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey("datasets.id"), nullable=True, index=True)
    period_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    period_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    as_of_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    metric_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    metric_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    statement_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    value_number: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    evidence_tier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provenance_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    company: Mapped["Company"] = relationship()
    document: Mapped["Document"] = relationship()
    source: Mapped["Source"] = relationship()
    ingestion_run: Mapped["IngestionRun"] = relationship()
    dataset: Mapped["Dataset"] = relationship()


class RoadmapEntry(Base):
    """Atomic forward-looking roadmap notch with source and model posture."""

    __tablename__ = "roadmap_entries"
    __table_args__ = (
        UniqueConstraint("entry_key", name="uq_roadmap_entries_entry_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    owner: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    expected_window: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    target_year: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    target_quarter: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    stance: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    confidence_label: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_use: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    materiality: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    applies_to: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    affected_models: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    beneficiaries: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    dependencies: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reiteration_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_material_change_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    source_document: Mapped["Document | None"] = relationship()
    mentions: Mapped[list["RoadmapMention"]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
    )
    view_links: Mapped[list["RoadmapEntryView"]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
    )


class RoadmapMention(Base):
    """Evidence, reiteration, contradiction, or cloud sighting attached to a roadmap entry."""

    __tablename__ = "roadmap_mentions"
    __table_args__ = (
        UniqueConstraint("mention_key", name="uq_roadmap_mentions_mention_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mention_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("roadmap_entries.id"), nullable=False, index=True)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    cloud_goal_run_id: Mapped[int | None] = mapped_column(ForeignKey("cloud_goal_runs.id"), nullable=True, index=True)
    mention_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="observation", index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_owner: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    expected_window: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stance: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    confidence_label: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    materiality: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    evidence_tier: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    entry: Mapped["RoadmapEntry"] = relationship(back_populates="mentions")
    source_document: Mapped["Document | None"] = relationship()
    cloud_goal_run: Mapped["CloudGoalRun | None"] = relationship()


class RoadmapView(Base):
    """Named lens over roadmap entries, such as Memory, Semicap, Nvidia, or a company view."""

    __tablename__ = "roadmap_views"
    __table_args__ = (
        UniqueConstraint("view_key", name="uq_roadmap_views_view_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    view_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    entry_links: Mapped[list["RoadmapEntryView"]] = relationship(
        back_populates="view",
        cascade="all, delete-orphan",
    )


class RoadmapEntryView(Base):
    """Many-to-many mapping that lets one atomic entry appear in multiple roadmap lenses."""

    __tablename__ = "roadmap_entry_views"
    __table_args__ = (
        UniqueConstraint("entry_id", "view_id", name="uq_roadmap_entry_views_entry_view"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("roadmap_entries.id"), nullable=False, index=True)
    view_id: Mapped[int] = mapped_column(ForeignKey("roadmap_views.id"), nullable=False, index=True)
    relation: Mapped[str] = mapped_column(String(32), nullable=False, default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    entry: Mapped["RoadmapEntry"] = relationship(back_populates="view_links")
    view: Mapped["RoadmapView"] = relationship(back_populates="entry_links")


class InformalResearchDocument(Base):
    """Loose research note for useful synthesis that is not ready for a canonical rail."""

    __tablename__ = "informal_research_documents"
    __table_args__ = (
        UniqueConstraint("doc_key", name="uq_informal_research_documents_doc_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_run_id: Mapped[int | None] = mapped_column(ForeignKey("cloud_goal_runs.id"), nullable=True, index=True)
    source_operation_index: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    failed_operation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    promotion_target: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_material_update_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    source_run: Mapped["CloudGoalRun | None"] = relationship()
    events: Mapped[list["InformalResearchDocumentEvent"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )


class InformalResearchDocumentEvent(Base):
    """Append-only event trail for an informal research document."""

    __tablename__ = "informal_research_document_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("informal_research_documents.id"), nullable=False, index=True)
    source_run_id: Mapped[int | None] = mapped_column(ForeignKey("cloud_goal_runs.id"), nullable=True, index=True)
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_operation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    document: Mapped["InformalResearchDocument"] = relationship(back_populates="events")
    source_run: Mapped["CloudGoalRun | None"] = relationship()


class InstrumentRequest(Base):
    """Structured queue object for chart, table, tracker, or dataset package requests."""

    __tablename__ = "instrument_requests"
    __table_args__ = (
        UniqueConstraint("request_key", name="uq_instrument_requests_request_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_key: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(String(64), nullable=False, default="instrument")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_entities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    target_themes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    desired_outputs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    evidence_bundle: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    provenance_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_tier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    automation_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_kind: Mapped[str | None] = mapped_column(String(128), nullable=True)
    draft_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    live_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    linked_objects: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ProductStageEntry(Base):
    """Tracked product-stage state with evidence, dates, and linked research objects."""

    __tablename__ = "product_stage_entries"
    __table_args__ = (
        UniqueConstraint("entry_key", name="uq_product_stage_entries_entry_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    tracker_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    date_precision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    date_text: Mapped[str | None] = mapped_column(String(128), nullable=True)
    certainty: Mapped[str] = mapped_column(String(32), nullable=False, default="attributed")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_tier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_bundle: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    linked_dataset_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    linked_supply_chain_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    linked_note_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    linked_thesis_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    candidate_claim_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    resolved_claim_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conflict_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    highest_evidence_tier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_claim_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    company: Mapped["Company"] = relationship()
    claims: Mapped[list["ProductStageClaim"]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
    )
    history_rows: Mapped[list["ProductStageHistory"]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
    )


class ProductStageClaim(Base):
    """Attributed stage claim feeding the resolved current stage view."""

    __tablename__ = "product_stage_claims"
    __table_args__ = (
        UniqueConstraint("claim_key", name="uq_product_stage_claims_claim_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("product_stage_entries.id"), nullable=False, index=True)
    claim_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    date_precision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    date_text: Mapped[str | None] = mapped_column(String(128), nullable=True)
    certainty: Mapped[str] = mapped_column(String(32), nullable=False, default="attributed")
    evidence_tier: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    provenance_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    source_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    linked_dataset_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    linked_supply_chain_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    linked_note_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    linked_thesis_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    automation_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    entry: Mapped["ProductStageEntry"] = relationship(back_populates="claims")
    source_document: Mapped["Document"] = relationship()


class ProductStageHistory(Base):
    """Audit trail for product-stage updates and review events."""

    __tablename__ = "product_stage_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("product_stage_entries.id"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    current_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    entry: Mapped["ProductStageEntry"] = relationship(back_populates="history_rows")
    source_document: Mapped["Document"] = relationship()


class ResearchSubject(Base):
    __tablename__ = "research_subjects"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_research_subjects_slug"),
        UniqueConstraint("subject_key", name="uq_research_subjects_subject_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    subject_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    primary_entity_id: Mapped[int | None] = mapped_column(ForeignKey("entities.id"), nullable=True, index=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    issuer: Mapped["Issuer | None"] = relationship()
    company: Mapped["Company | None"] = relationship()
    primary_entity: Mapped["Entity | None"] = relationship()
    aliases: Mapped[list["ResearchSubjectAlias"]] = relationship(
        back_populates="subject",
        cascade="all, delete-orphan",
    )
    outgoing_relations: Mapped[list["ResearchSubjectRelation"]] = relationship(
        back_populates="source_subject",
        foreign_keys="ResearchSubjectRelation.source_subject_id",
        cascade="all, delete-orphan",
    )
    incoming_relations: Mapped[list["ResearchSubjectRelation"]] = relationship(
        back_populates="target_subject",
        foreign_keys="ResearchSubjectRelation.target_subject_id",
        cascade="all, delete-orphan",
    )
    institutional_identity: Mapped["EntityIdentity | None"] = relationship(back_populates="subject", uselist=False)
    institutional_sites: Mapped[list["Site"]] = relationship(back_populates="subject")
    institutional_programs: Mapped[list["Program"]] = relationship(back_populates="subject")


class ResearchSubjectAlias(Base):
    __tablename__ = "research_subject_aliases"
    __table_args__ = (
        UniqueConstraint("subject_id", "alias", name="uq_research_subject_aliases_subject_alias"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("research_subjects.id"), nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    alias_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject"] = relationship(back_populates="aliases")


class ResearchSubjectRelation(Base):
    __tablename__ = "research_subject_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_subject_id",
            "target_subject_id",
            "relation_kind",
            name="uq_research_subject_relations_pair_kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_subject_id: Mapped[int] = mapped_column(ForeignKey("research_subjects.id"), nullable=False, index=True)
    target_subject_id: Mapped[int] = mapped_column(ForeignKey("research_subjects.id"), nullable=False, index=True)
    relation_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    source_subject: Mapped["ResearchSubject"] = relationship(
        back_populates="outgoing_relations",
        foreign_keys=[source_subject_id],
    )
    target_subject: Mapped["ResearchSubject"] = relationship(
        back_populates="incoming_relations",
        foreign_keys=[target_subject_id],
    )


class ResearchSkill(Base):
    __tablename__ = "research_skills"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_research_skills_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    universe_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    inputs_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    trigger_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    scoring_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    output_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ResearchExtension(Base):
    """One scoped research capability and its currently adopted version."""

    __tablename__ = "research_extensions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    capability: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    active_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ResearchExtensionVersion(Base):
    """Immutable candidate content; validation and adoption are separate events."""

    __tablename__ = "research_extension_versions"
    __table_args__ = (UniqueConstraint("extension_id", "version", name="uq_research_extension_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    extension_id: Mapped[int] = mapped_column(ForeignKey("research_extensions.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ResearchExtensionEvent(Base):
    """Idempotent audit receipt for every candidate lifecycle transition."""

    __tablename__ = "research_extension_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True, index=True)
    extension_id: Mapped[int] = mapped_column(ForeignKey("research_extensions.id"), nullable=False, index=True)
    candidate_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    cloud_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prior_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    receipt_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ResearchPlay(Base):
    __tablename__ = "research_plays"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_research_plays_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    universe_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    schedule_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    alert_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    skills: Mapped[list["ResearchPlaySkill"]] = relationship(
        back_populates="play",
        cascade="all, delete-orphan",
    )
    runs: Mapped[list["ResearchPlayRun"]] = relationship(
        back_populates="play",
        cascade="all, delete-orphan",
    )


class ResearchPlaySkill(Base):
    __tablename__ = "research_play_skills"
    __table_args__ = (
        UniqueConstraint("play_id", "skill_id", name="uq_research_play_skills_play_skill"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    play_id: Mapped[int] = mapped_column(ForeignKey("research_plays.id"), nullable=False, index=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("research_skills.id"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    play: Mapped["ResearchPlay"] = relationship(back_populates="skills")
    skill: Mapped["ResearchSkill"] = relationship()


class ResearchPlayRun(Base):
    __tablename__ = "research_play_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    play_id: Mapped[int] = mapped_column(ForeignKey("research_plays.id"), nullable=False, index=True)
    trigger_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    trigger_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    play: Mapped["ResearchPlay"] = relationship(back_populates="runs")


class ResearchPlaySignal(Base):
    __tablename__ = "research_play_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    play_id: Mapped[int] = mapped_column(ForeignKey("research_plays.id"), nullable=False, index=True)
    play_run_id: Mapped[int | None] = mapped_column(ForeignKey("research_play_runs.id"), nullable=True, index=True)
    skill_id: Mapped[int | None] = mapped_column(ForeignKey("research_skills.id"), nullable=True, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    signal_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    signal_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    observed_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    play: Mapped["ResearchPlay"] = relationship()
    play_run: Mapped["ResearchPlayRun | None"] = relationship()
    skill: Mapped["ResearchSkill | None"] = relationship()
    subject: Mapped["ResearchSubject | None"] = relationship()


class AlignmentFeedback(Base):
    __tablename__ = "alignment_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    feedback_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class EntityIdentity(Base):
    __tablename__ = "entity_identities"
    __table_args__ = (
        UniqueConstraint("subject_id", name="uq_entity_identities_subject"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    canonical_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="entity")
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    jurisdiction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject | None"] = relationship(back_populates="institutional_identity")
    issuer: Mapped["Issuer | None"] = relationship()
    company: Mapped["Company | None"] = relationship()
    aliases: Mapped[list["EntityIdentityAlias"]] = relationship(
        back_populates="identity",
        cascade="all, delete-orphan",
    )
    external_ids: Mapped[list["EntityExternalId"]] = relationship(
        back_populates="identity",
        cascade="all, delete-orphan",
    )
    sites: Mapped[list["Site"]] = relationship(back_populates="identity")


class EntityIdentityAlias(Base):
    __tablename__ = "entity_identity_aliases"
    __table_args__ = (
        UniqueConstraint("identity_id", "alias", "alias_type", name="uq_entity_identity_aliases_identity_alias"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity_id: Mapped[int] = mapped_column(ForeignKey("entity_identities.id"), nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    alias_type: Mapped[str] = mapped_column(String(32), nullable=False, default="name")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    identity: Mapped["EntityIdentity"] = relationship(back_populates="aliases")


class EntityExternalId(Base):
    __tablename__ = "entity_external_ids"
    __table_args__ = (
        UniqueConstraint("identity_id", "provider", "external_id", name="uq_entity_external_ids_identity_provider_value"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity_id: Mapped[int] = mapped_column(ForeignKey("entity_identities.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    identity: Mapped["EntityIdentity"] = relationship(back_populates="external_ids")


class Site(Base):
    __tablename__ = "sites"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_sites_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    identity_id: Mapped[int | None] = mapped_column(ForeignKey("entity_identities.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    site_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="facility")
    operator_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    utility_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    locality: Mapped[str | None] = mapped_column(String(128), nullable=True)
    region: Mapped[str | None] = mapped_column(String(128), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    parcel_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject | None"] = relationship(back_populates="institutional_sites")
    issuer: Mapped["Issuer | None"] = relationship()
    company: Mapped["Company | None"] = relationship()
    identity: Mapped["EntityIdentity | None"] = relationship(back_populates="sites")
    programs: Mapped[list["Program"]] = relationship(back_populates="site")


class Program(Base):
    __tablename__ = "programs"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_programs_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject | None"] = relationship(back_populates="institutional_programs")
    issuer: Mapped["Issuer | None"] = relationship()
    company: Mapped["Company | None"] = relationship()
    site: Mapped["Site | None"] = relationship(back_populates="programs")
    nodes: Mapped[list["ProgramNode"]] = relationship(back_populates="program", cascade="all, delete-orphan")
    edges: Mapped[list["ProgramEdge"]] = relationship(back_populates="program", cascade="all, delete-orphan")


class ProgramNode(Base):
    __tablename__ = "program_nodes"
    __table_args__ = (
        UniqueConstraint("program_id", "node_key", name="uq_program_nodes_program_node_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("programs.id"), nullable=False, index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"), nullable=True, index=True)
    node_key: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    node_type: Mapped[str] = mapped_column(String(64), nullable=False, default="component")
    dependency_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manufacturer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    constraining_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    attachment_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    program: Mapped["Program"] = relationship(back_populates="nodes")
    site: Mapped["Site | None"] = relationship()


class ProgramEdge(Base):
    __tablename__ = "program_edges"
    __table_args__ = (
        UniqueConstraint("program_id", "source_node_id", "target_node_id", "edge_kind", name="uq_program_edges_program_pair_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("programs.id"), nullable=False, index=True)
    source_node_id: Mapped[int] = mapped_column(ForeignKey("program_nodes.id"), nullable=False, index=True)
    target_node_id: Mapped[int] = mapped_column(ForeignKey("program_nodes.id"), nullable=False, index=True)
    edge_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="depends_on")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    program: Mapped["Program"] = relationship(back_populates="edges")
    source_node: Mapped["ProgramNode"] = relationship(foreign_keys=[source_node_id])
    target_node: Mapped["ProgramNode"] = relationship(foreign_keys=[target_node_id])


class InstitutionalEvidenceItem(Base):
    __tablename__ = "institutional_evidence_items"
    __table_args__ = (
        UniqueConstraint("evidence_key", name="uq_institutional_evidence_items_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    evidence_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True, index=True)
    source_profile_id: Mapped[int | None] = mapped_column(ForeignKey("source_profiles.source_id"), nullable=True, index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"), nullable=True, index=True)
    program_id: Mapped[int | None] = mapped_column(ForeignKey("programs.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="document")
    provenance_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rights_class: Mapped[str] = mapped_column(String(32), nullable=False, default="public")
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    source: Mapped["Source | None"] = relationship(foreign_keys=[source_id])
    source_profile: Mapped["SourceProfile | None"] = relationship(foreign_keys=[source_profile_id])
    document: Mapped["Document | None"] = relationship()
    subject: Mapped["ResearchSubject | None"] = relationship()
    issuer: Mapped["Issuer | None"] = relationship()
    company: Mapped["Company | None"] = relationship()
    site: Mapped["Site | None"] = relationship()
    program: Mapped["Program | None"] = relationship()
    observations: Mapped[list["InstitutionalObservation"]] = relationship(
        back_populates="evidence",
        cascade="all, delete-orphan",
    )


class InstitutionalObservation(Base):
    __tablename__ = "institutional_observations"
    __table_args__ = (
        UniqueConstraint("observation_key", name="uq_institutional_observations_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observation_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("institutional_evidence_items.id"), nullable=True, index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"), nullable=True, index=True)
    program_id: Mapped[int | None] = mapped_column(ForeignKey("programs.id"), nullable=True, index=True)
    dataset_family: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    metric_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    value_number: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    period_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    as_of_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="accepted")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    evidence: Mapped["InstitutionalEvidenceItem | None"] = relationship(back_populates="observations")
    source: Mapped["Source | None"] = relationship()
    subject: Mapped["ResearchSubject | None"] = relationship()
    issuer: Mapped["Issuer | None"] = relationship()
    company: Mapped["Company | None"] = relationship()
    site: Mapped["Site | None"] = relationship()
    program: Mapped["Program | None"] = relationship()


class StateVectorSnapshot(Base):
    __tablename__ = "state_vector_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_key", name="uq_state_vector_snapshots_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False, default="subject")
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"), nullable=True, index=True)
    program_id: Mapped[int | None] = mapped_column(ForeignKey("programs.id"), nullable=True, index=True)
    program_node_id: Mapped[int | None] = mapped_column(ForeignKey("program_nodes.id"), nullable=True, index=True)
    installed_capacity: Mapped[float | None] = mapped_column(Float, nullable=True)
    effective_capacity: Mapped[float | None] = mapped_column(Float, nullable=True)
    utilization: Mapped[float | None] = mapped_column(Float, nullable=True)
    yield_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    lead_time_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    inventory_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    allocation_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qualification_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    demand_signal: Mapped[str | None] = mapped_column(String(64), nullable=True)
    schedule_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    logistics_friction: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    freshness_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    contradiction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject | None"] = relationship()
    site: Mapped["Site | None"] = relationship()
    program: Mapped["Program | None"] = relationship()
    program_node: Mapped["ProgramNode | None"] = relationship()


class CoverageGap(Base):
    __tablename__ = "coverage_gaps"
    __table_args__ = (
        UniqueConstraint("gap_key", name="uq_coverage_gaps_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gap_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"), nullable=True, index=True)
    program_id: Mapped[int | None] = mapped_column(ForeignKey("programs.id"), nullable=True, index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    gap_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    severity: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_rails: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    owner_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    owner_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject | None"] = relationship()
    site: Mapped["Site | None"] = relationship()
    program: Mapped["Program | None"] = relationship()
    source: Mapped["Source | None"] = relationship()


class ResearchSubjectScenario(Base):
    __tablename__ = "research_subject_scenarios"
    __table_args__ = (
        UniqueConstraint("subject_id", "slug", name="uq_research_subject_scenarios_subject_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("research_subjects.id"), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    scenario_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="custom", index=True)
    probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    selected_canon: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    thesis_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    assumptions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    key_metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    evidence_links_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    invalidation_triggers_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    open_questions_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    supersedes_scenario_id: Mapped[int | None] = mapped_column(ForeignKey("research_subject_scenarios.id"), nullable=True)
    audit_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject"] = relationship()
    supersedes_scenario: Mapped["ResearchSubjectScenario | None"] = relationship(remote_side=[id])


class ProductModelRevision(Base):
    """Immutable calculation and dependency definition for a product boundary."""

    __tablename__ = "product_model_revisions"
    __table_args__ = (UniqueConstraint("model_key", "version", name="uq_product_model_revision"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    boundary: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    variables_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    outputs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    bridges_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    premises_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    source_document_ids_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    change_summary: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("product_model_revisions.id"), nullable=True)
    created_by_run_id: Mapped[int | None] = mapped_column(ForeignKey("cloud_goal_runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ProductConfigurationRevision(Base):
    """Immutable product-specific inputs pinned to one calculation revision."""

    __tablename__ = "product_configuration_revisions"
    __table_args__ = (UniqueConstraint("configuration_key", "version", name="uq_product_configuration_revision"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    configuration_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    product_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    period: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    period_months: Mapped[float | None] = mapped_column(Float, nullable=True)
    world_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    model_revision_id: Mapped[int] = mapped_column(ForeignKey("product_model_revisions.id"), nullable=False, index=True)
    inputs_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_document_ids_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    change_summary: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("product_configuration_revisions.id"), nullable=True)
    forked_from_id: Mapped[int | None] = mapped_column(ForeignKey("product_configuration_revisions.id"), nullable=True)
    created_by_run_id: Mapped[int | None] = mapped_column(ForeignKey("cloud_goal_runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    model_revision: Mapped[ProductModelRevision] = relationship()


class ResearchSourcePack(Base):
    __tablename__ = "research_source_packs"
    __table_args__ = (
        UniqueConstraint("subject_id", name="uq_research_source_packs_subject"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("research_subjects.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="missing", index=True)
    checklist_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    items_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    missing_items_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    official_links_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    source_counts_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    audit_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject"] = relationship()


class ResearchReportQuality(Base):
    __tablename__ = "research_report_quality"
    __table_args__ = (
        UniqueConstraint("subject_id", name="uq_research_report_quality_subject"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("research_subjects.id"), nullable=False, index=True)
    report_id: Mapped[int | None] = mapped_column(ForeignKey("research_reports.id"), nullable=True, index=True)
    report_version_id: Mapped[int | None] = mapped_column(ForeignKey("research_report_versions.id"), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="missing", index=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    stale_as_of_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    audit_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject"] = relationship()
    report: Mapped["ResearchReport | None"] = relationship()
    report_version: Mapped["ResearchReportVersion | None"] = relationship()


class ResearchCampaign(Base):
    __tablename__ = "research_campaigns"
    __table_args__ = (
        UniqueConstraint("campaign_key", name="uq_research_campaigns_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    universe_id: Mapped[int | None] = mapped_column(ForeignKey("research_universes.id"), nullable=True, index=True)
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    universe: Mapped["ResearchUniverse | None"] = relationship()
    items: Mapped[list["ResearchCampaignItem"]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
    )


class ResearchCampaignItem(Base):
    __tablename__ = "research_campaign_items"
    __table_args__ = (
        UniqueConstraint("campaign_id", "target_type", "target_key", name="uq_research_campaign_items_target"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("research_campaigns.id"), nullable=False, index=True)
    item_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    target_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    universe_id: Mapped[int | None] = mapped_column(ForeignKey("research_universes.id"), nullable=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    blocking_gaps_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    assigned_rail: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_report_id: Mapped[int | None] = mapped_column(ForeignKey("research_reports.id"), nullable=True)
    output_report_version_id: Mapped[int | None] = mapped_column(ForeignKey("research_report_versions.id"), nullable=True)
    quality_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    campaign: Mapped["ResearchCampaign"] = relationship(back_populates="items")
    subject: Mapped["ResearchSubject | None"] = relationship()
    universe: Mapped["ResearchUniverse | None"] = relationship()
    output_report: Mapped["ResearchReport | None"] = relationship(foreign_keys=[output_report_id])
    output_report_version: Mapped["ResearchReportVersion | None"] = relationship(foreign_keys=[output_report_version_id])


class AgentCanonCard(Base):
    __tablename__ = "agent_canon_cards"
    __table_args__ = (
        UniqueConstraint("subject_id", name="uq_agent_canon_cards_subject"),
        UniqueConstraint("card_key", name="uq_agent_canon_cards_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("research_subjects.id"), nullable=False, index=True)
    card_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_report_id: Mapped[int | None] = mapped_column(ForeignKey("research_reports.id"), nullable=True)
    source_report_version_id: Mapped[int | None] = mapped_column(ForeignKey("research_report_versions.id"), nullable=True)
    scenario_id: Mapped[int | None] = mapped_column(ForeignKey("research_subject_scenarios.id"), nullable=True)
    source_pack_id: Mapped[int | None] = mapped_column(ForeignKey("research_source_packs.id"), nullable=True)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    audit_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    generated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject"] = relationship()
    source_report: Mapped["ResearchReport | None"] = relationship(foreign_keys=[source_report_id])
    source_report_version: Mapped["ResearchReportVersion | None"] = relationship(foreign_keys=[source_report_version_id])
    scenario: Mapped["ResearchSubjectScenario | None"] = relationship()
    source_pack: Mapped["ResearchSourcePack | None"] = relationship()


class BottleneckModel(Base):
    __tablename__ = "bottleneck_models"
    __table_args__ = (
        UniqueConstraint("model_key", name="uq_bottleneck_models_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(64), nullable=False, default="general", index=True)
    scope_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"), nullable=True, index=True)
    program_id: Mapped[int | None] = mapped_column(ForeignKey("programs.id"), nullable=True, index=True)
    supply_chain_id: Mapped[int | None] = mapped_column(ForeignKey("supply_chains.id"), nullable=True, index=True)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("supply_chain_snapshots.id"), nullable=True, index=True)
    constrained_input: Mapped[str | None] = mapped_column(String(255), nullable=True)
    output_unit: Mapped[str | None] = mapped_column(String(128), nullable=True)
    regime: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    thesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    qualified_supplier_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supplier_qualification_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject | None"] = relationship()
    site: Mapped["Site | None"] = relationship()
    program: Mapped["Program | None"] = relationship()
    supply_chain: Mapped["SupplyChain | None"] = relationship()
    snapshot: Mapped["SupplyChainSnapshot | None"] = relationship()
    assessments: Mapped[list["BottleneckAssessment"]] = relationship(back_populates="model")


class BottleneckModelLink(Base):
    __tablename__ = "bottleneck_model_links"
    __table_args__ = (
        UniqueConstraint("model_id", "object_type", "object_id", "relation", name="uq_bottleneck_model_links_target"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("bottleneck_models.id"), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    object_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    relation: Mapped[str] = mapped_column(String(64), nullable=False, default="references", index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    model: Mapped["BottleneckModel"] = relationship()


class BottleneckAssessment(Base):
    __tablename__ = "bottleneck_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_id: Mapped[int | None] = mapped_column(ForeignKey("bottleneck_models.id"), nullable=True, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"), nullable=True, index=True)
    program_id: Mapped[int | None] = mapped_column(ForeignKey("programs.id"), nullable=True, index=True)
    supply_chain_id: Mapped[int | None] = mapped_column(ForeignKey("supply_chains.id"), nullable=True, index=True)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("supply_chain_snapshots.id"), nullable=True, index=True)
    scope_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    scope_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    active_node_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shadow_node_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    constrained_input: Mapped[str | None] = mapped_column(String(255), nullable=True)
    output_unit: Mapped[str | None] = mapped_column(String(128), nullable=True)
    regime: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    classification: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    bindingness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    shadow_price_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ramp_resistance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    substitutability_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    persistence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    latent_demand_absorption_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    qualified_supplier_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    marginal_system_leverage: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    supplier_qualification_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    scorecard_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    model: Mapped["BottleneckModel | None"] = relationship(back_populates="assessments")
    subject: Mapped["ResearchSubject | None"] = relationship()
    site: Mapped["Site | None"] = relationship()
    program: Mapped["Program | None"] = relationship()
    supply_chain: Mapped["SupplyChain | None"] = relationship()
    snapshot: Mapped["SupplyChainSnapshot | None"] = relationship()


class ComplianceReview(Base):
    __tablename__ = "compliance_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    object_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source_profile_id: Mapped[int | None] = mapped_column(ForeignKey("source_profiles.source_id"), nullable=True, index=True)
    rights_class: Mapped[str] = mapped_column(String(32), nullable=False, default="public")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="approved")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    source_profile: Mapped["SourceProfile | None"] = relationship()


class ExpertInteraction(Base):
    __tablename__ = "expert_interactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    program_id: Mapped[int | None] = mapped_column(ForeignKey("programs.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    counterparty: Mapped[str | None] = mapped_column(String(255), nullable=True)
    interaction_type: Mapped[str] = mapped_column(String(32), nullable=False, default="expert_call")
    interaction_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    restrictions: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="logged")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject | None"] = relationship()
    program: Mapped["Program | None"] = relationship()


class TranscriptSummary(Base):
    __tablename__ = "transcript_summaries"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_transcript_summaries_document"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    ticker: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    fiscal_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fiscal_quarter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    period_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary_md: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    document: Mapped["Document"] = relationship()
    issuer: Mapped["Issuer | None"] = relationship()
    company: Mapped["Company | None"] = relationship()


class TranscriptWatchState(Base):
    __tablename__ = "transcript_watch_states"
    __table_args__ = (
        UniqueConstraint("watch_key", name="uq_transcript_watch_states_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    latest_filing_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    latest_filing_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    latest_filing_doc_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5, index=True)
    miss_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_found_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transcript_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("ir_artifacts.id"), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    company: Mapped["Company | None"] = relationship(foreign_keys=[company_id])
    issuer: Mapped["Issuer | None"] = relationship(foreign_keys=[issuer_id])
    latest_filing_document: Mapped["Document | None"] = relationship(foreign_keys=[latest_filing_document_id])
    transcript_artifact: Mapped["IRArtifact | None"] = relationship(foreign_keys=[transcript_artifact_id])


class ResearchReport(Base):
    __tablename__ = "research_reports"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_research_reports_slug"),
        UniqueConstraint("subject_key", name="uq_research_reports_subject_key"),
        UniqueConstraint("issuer_id", name="uq_research_reports_issuer_id"),
        UniqueConstraint("company_id", name="uq_research_reports_company_id"),
        UniqueConstraint("subject_id", name="uq_research_reports_subject_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    subject_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    latest_published_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latest_subject_slug: Mapped[str | None] = mapped_column(String(128), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject | None"] = relationship()
    issuer: Mapped["Issuer | None"] = relationship()
    company: Mapped["Company | None"] = relationship()
    versions: Mapped[list["ResearchReportVersion"]] = relationship(
        back_populates="report",
        cascade="all, delete-orphan",
    )


class ResearchReportVersion(Base):
    __tablename__ = "research_report_versions"
    __table_args__ = (
        UniqueConstraint("report_id", "version", name="uq_research_report_versions_report_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("research_reports.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="building")
    trigger_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trigger_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trigger_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    change_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    coverage_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    as_of_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    report: Mapped["ResearchReport"] = relationship(back_populates="versions")
    artifacts: Mapped[list["ResearchReportArtifact"]] = relationship(
        back_populates="report_version",
        cascade="all, delete-orphan",
    )


class ResearchReportArtifact(Base):
    __tablename__ = "research_report_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "report_version_id",
            "artifact_type",
            "variant",
            name="uq_research_report_artifacts_version_type_variant",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_version_id: Mapped[int] = mapped_column(ForeignKey("research_report_versions.id"), nullable=False, index=True)
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    variant: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    object_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    report_version: Mapped["ResearchReportVersion"] = relationship(back_populates="artifacts")


class KnowledgeAsset(Base):
    __tablename__ = "knowledge_assets"
    __table_args__ = (
        UniqueConstraint("subject_id", "kind", name="uq_knowledge_assets_subject_kind"),
        UniqueConstraint("slug", name="uq_knowledge_assets_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("research_subjects.id"), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # primer | teardown
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    stale_after_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latest_published_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_stale_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stale_check_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    subject: Mapped["ResearchSubject"] = relationship()
    versions: Mapped[list["KnowledgeAssetVersion"]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
    )
    links: Mapped[list["KnowledgeAssetLink"]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
    )


class KnowledgeAssetVersion(Base):
    __tablename__ = "knowledge_asset_versions"
    __table_args__ = (
        UniqueConstraint("asset_id", "version", name="uq_knowledge_asset_versions_asset_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("knowledge_assets.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # draft | published
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    coverage_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    evidence_bundle: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    ai_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    as_of_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    asset: Mapped["KnowledgeAsset"] = relationship(back_populates="versions")


class KnowledgeAssetLink(Base):
    __tablename__ = "knowledge_asset_links"
    __table_args__ = (
        UniqueConstraint(
            "asset_id",
            "object_type",
            "object_id",
            "relation",
            name="uq_knowledge_asset_links_asset_object_relation",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("knowledge_assets.id"), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    object_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    relation: Mapped[str] = mapped_column(String(32), nullable=False, default="references")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    asset: Mapped["KnowledgeAsset"] = relationship(back_populates="links")


class FreshnessReview(Base):
    __tablename__ = "freshness_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    object_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("research_subjects.id"), nullable=True, index=True)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    trigger_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_bundle: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    live_web_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    subject: Mapped["ResearchSubject | None"] = relationship()


class SupplyChain(Base):
    """
    Supply chain graph model (layered nodes + edges) for product/industry modeling.
    """
    __tablename__ = "supply_chains"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_supply_chains_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    product: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SupplyChainSnapshot(Base):
    """Normalized versioned snapshot for supply-chain modeling and publishing."""

    __tablename__ = "supply_chain_snapshots"
    __table_args__ = (
        UniqueConstraint("chain_id", "version", "status", name="uq_supply_chain_snapshots_chain_version_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("supply_chains.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # draft | published
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    based_on_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("supply_chain_snapshots.id"), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)  # layout/layer metadata
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    nodes: Mapped[list["SupplyChainNode"]] = relationship(back_populates="snapshot", cascade="all, delete-orphan")
    edges: Mapped[list["SupplyChainEdge"]] = relationship(back_populates="snapshot", cascade="all, delete-orphan")
    metric_bindings: Mapped[list["SupplyChainMetricBinding"]] = relationship(back_populates="snapshot", cascade="all, delete-orphan")


class SupplyChainNode(Base):
    __tablename__ = "supply_chain_nodes"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "node_key", name="uq_supply_chain_nodes_snapshot_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("supply_chain_snapshots.id"), nullable=False, index=True)
    node_key: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    layer_key: Mapped[str] = mapped_column(String(128), nullable=False)
    node_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manufacturer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    capacity_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    capacity_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lead_time_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pricing_power: Mapped[float | None] = mapped_column(Float, nullable=True)
    bottleneck: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    snapshot: Mapped["SupplyChainSnapshot"] = relationship(back_populates="nodes")


class SupplyChainEdge(Base):
    __tablename__ = "supply_chain_edges"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "source_node_id", "target_node_id", "edge_type", name="uq_supply_chain_edges_snapshot_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("supply_chain_snapshots.id"), nullable=False, index=True)
    source_node_id: Mapped[int] = mapped_column(ForeignKey("supply_chain_nodes.id"), nullable=False, index=True)
    target_node_id: Mapped[int] = mapped_column(ForeignKey("supply_chain_nodes.id"), nullable=False, index=True)
    edge_type: Mapped[str] = mapped_column(String(64), nullable=False, default="materials")
    criticality: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    snapshot: Mapped["SupplyChainSnapshot"] = relationship(back_populates="edges")
    source_node: Mapped["SupplyChainNode"] = relationship(foreign_keys=[source_node_id])
    target_node: Mapped["SupplyChainNode"] = relationship(foreign_keys=[target_node_id])


class SupplyChainScenario(Base):
    __tablename__ = "supply_chain_scenarios"
    __table_args__ = (
        UniqueConstraint("chain_id", "slug", name="uq_supply_chain_scenarios_chain_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("supply_chains.id"), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    base_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("supply_chain_snapshots.id"), nullable=True)
    draft_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("supply_chain_snapshots.id"), nullable=True)
    published_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("supply_chain_snapshots.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SupplyChainMetricBinding(Base):
    __tablename__ = "supply_chain_metric_bindings"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "scope_type",
            "scope_key",
            "metric_kind",
            "dataset_id",
            "dataset_view_id",
            "column_key",
            name="uq_supply_chain_metric_bindings_scope_metric",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("supply_chain_snapshots.id"), nullable=False, index=True)
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey("datasets.id"), nullable=True, index=True)
    dataset_view_id: Mapped[int | None] = mapped_column(ForeignKey("dataset_views.id"), nullable=True, index=True)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False, default="chain")  # chain | node | edge
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False, default="chain")
    metric_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    column_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    aggregation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    snapshot: Mapped["SupplyChainSnapshot"] = relationship(back_populates="metric_bindings")
    dataset: Mapped["Dataset | None"] = relationship(back_populates="metric_bindings")
    dataset_view: Mapped["DatasetView | None"] = relationship(back_populates="metric_bindings")


class SupplyChainAlertRule(Base):
    __tablename__ = "supply_chain_alert_rules"
    __table_args__ = (
        UniqueConstraint("chain_id", "scope_type", "scope_key", "name", name="uq_supply_chain_alert_rules_scope_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("supply_chains.id"), nullable=False, index=True)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False, default="chain")
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False, default="chain")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    metric_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    operator: Mapped[str] = mapped_column(String(16), nullable=False, default="gte")
    threshold_value: Mapped[float] = mapped_column(Float, nullable=False)
    window_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SupplyChainLink(Base):
    __tablename__ = "supply_chain_links"
    __table_args__ = (
        UniqueConstraint("chain_id", "object_type", "object_id", "relation", name="uq_supply_chain_links_target"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("supply_chains.id"), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)  # thesis | note
    object_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    relation: Mapped[str] = mapped_column(String(32), nullable=False, default="context")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SupplyChainVersion(Base):
    """
    Versioned snapshots for supply chain models.
    Draft versions track latest references; published versions pin references.
    """
    __tablename__ = "supply_chain_versions"
    __table_args__ = (
        UniqueConstraint("chain_id", "version", name="uq_supply_chain_versions_chain_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("supply_chains.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # draft | published
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_version_id: Mapped[int | None] = mapped_column(ForeignKey("supply_chain_versions.id"), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SupplyChainRelation(Base):
    """
    Relations between supply chains (e.g., component-of / used-in).
    """
    __tablename__ = "supply_chain_relations"
    __table_args__ = (
        UniqueConstraint("parent_chain_id", "child_chain_id", "relation_type", name="uq_supply_chain_relations"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_chain_id: Mapped[int] = mapped_column(ForeignKey("supply_chains.id"), nullable=False, index=True)
    child_chain_id: Mapped[int] = mapped_column(ForeignKey("supply_chains.id"), nullable=False, index=True)
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False, default="component_of")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    net_liquidation: Mapped[float | None] = mapped_column(Float, nullable=True)
    cash: Mapped[float | None] = mapped_column(Float, nullable=True)
    gross_exposure: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_exposure: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    mtd_pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ytd_pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_snapshot_id: Mapped[int] = mapped_column(ForeignKey("portfolio_snapshots.id"), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    conid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    asset_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class IbkrRequestLog(Base):
    __tablename__ = "ibkr_request_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class PortfolioMoodSnapshot(Base):
    __tablename__ = "portfolio_mood_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    primary_mood: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    secondary_mood: Mapped[str | None] = mapped_column(String(32), nullable=True)
    intensity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    caption: Mapped[str] = mapped_column(Text, nullable=False)
    sprite_asset_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class IntelligenceEvent(Base):
    __tablename__ = "intelligence_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    dataset_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    note_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    thesis_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    document_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    importance_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    novelty_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class UserObjectView(Base):
    __tablename__ = "user_object_views"
    __table_args__ = (
        UniqueConstraint("user_id", "object_type", "object_id", name="uq_user_object_views_target"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, default="local")
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(128), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class UserTriageState(Base):
    __tablename__ = "user_triage_states"
    __table_args__ = (
        UniqueConstraint("user_id", "object_type", "object_id", name="uq_user_triage_states_target"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, default="local")
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="new", index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class ResearchInstrument(Base):
    """
    Local instrument registry for research universes.

    This is listing-level when a public security has a ticker/provider symbol, and
    entity-level for private companies until a public listing exists. Related
    listings connect through issuer_id where that linkage is known.
    """

    __tablename__ = "research_instruments"
    __table_args__ = (
        UniqueConstraint("instrument_key", name="uq_research_instruments_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ticker: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    exchange: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    security_type: Mapped[str] = mapped_column(String(32), nullable=False, default="STK", index=True)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    issuer_id: Mapped[int | None] = mapped_column(ForeignKey("issuers.id"), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    ibkr_conid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    yahoo_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    primary_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    issuer: Mapped["Issuer | None"] = relationship()
    company: Mapped["Company | None"] = relationship()
    identifiers: Mapped[list["ResearchInstrumentIdentifier"]] = relationship(
        back_populates="instrument",
        cascade="all, delete-orphan",
    )
    universe_rows: Mapped[list["ResearchUniverseRow"]] = relationship(back_populates="instrument")


class ResearchInstrumentIdentifier(Base):
    __tablename__ = "research_instrument_identifiers"
    __table_args__ = (
        UniqueConstraint("provider", "id_type", "id_value", name="uq_research_instrument_identifier"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("research_instruments.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    id_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    id_value: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    exchange: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    instrument: Mapped["ResearchInstrument"] = relationship(back_populates="identifiers")


class ResearchUniverse(Base):
    __tablename__ = "research_universes"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_research_universes_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    sections: Mapped[list["ResearchUniverseSection"]] = relationship(
        back_populates="universe",
        cascade="all, delete-orphan",
    )
    rows: Mapped[list["ResearchUniverseRow"]] = relationship(back_populates="universe")


class ResearchUniverseSection(Base):
    __tablename__ = "research_universe_sections"
    __table_args__ = (
        UniqueConstraint("universe_id", "section_key", name="uq_research_universe_sections_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("research_universes.id"), nullable=False, index=True)
    section_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    universe: Mapped["ResearchUniverse"] = relationship(back_populates="sections")
    rows: Mapped[list["ResearchUniverseRow"]] = relationship(
        back_populates="section",
        cascade="all, delete-orphan",
    )


class ResearchUniverseRow(Base):
    __tablename__ = "research_universe_rows"
    __table_args__ = (
        UniqueConstraint(
            "section_id",
            "instrument_id",
            "role",
            "display_label",
            name="uq_research_universe_rows_semantic_slot",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("research_universes.id"), nullable=False, index=True)
    section_id: Mapped[int] = mapped_column(ForeignKey("research_universe_sections.id"), nullable=False, index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("research_instruments.id"), nullable=False, index=True)
    display_label: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    exposure_type: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    exposure_strength: Mapped[str | None] = mapped_column(String(32), nullable=True)
    conviction: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    universe: Mapped["ResearchUniverse"] = relationship(back_populates="rows")
    section: Mapped["ResearchUniverseSection"] = relationship(back_populates="rows")
    instrument: Mapped["ResearchInstrument"] = relationship(back_populates="universe_rows")
