"""Database models and helpers for the loan decision system (SQLite via SQLAlchemy 2.0)."""
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import (JSON, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer,
                        String, Text, create_engine, event, inspect, make_url, text)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_URL = "sqlite:///loans.db"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Stores datetimes as UTC and always returns them timezone-aware.

    SQLite has no timezone support, so values come back naive without this.
    """
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime given; use timezone-aware UTC datetimes")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


def _in(column: str, values: tuple[str, ...]) -> CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=f"ck_{column}")


class HistoricalLoan(Base):
    """Mirrors the table created by load_data.py. create_all() leaves it untouched."""
    __tablename__ = "historical_loans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_age: Mapped[Optional[float]] = mapped_column(Float)
    person_gender: Mapped[Optional[str]] = mapped_column(Text)
    person_education: Mapped[Optional[str]] = mapped_column(Text)
    person_income: Mapped[Optional[float]] = mapped_column(Float)
    person_emp_exp: Mapped[Optional[int]] = mapped_column(Integer)
    person_home_ownership: Mapped[Optional[str]] = mapped_column(Text)
    loan_amnt: Mapped[Optional[float]] = mapped_column(Float)
    loan_intent: Mapped[Optional[str]] = mapped_column(Text)
    loan_int_rate: Mapped[Optional[float]] = mapped_column(Float)
    loan_percent_income: Mapped[Optional[float]] = mapped_column(Float)
    cb_person_cred_hist_length: Mapped[Optional[float]] = mapped_column(Float)
    credit_score: Mapped[Optional[int]] = mapped_column(Integer)
    previous_loan_defaults_on_file: Mapped[Optional[str]] = mapped_column(Text)
    loan_status: Mapped[Optional[int]] = mapped_column(Integer)


class Applicant(Base):
    """Age and gender are stored for fairness checks only, never as model features."""
    __tablename__ = "applicants"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200))
    age: Mapped[Optional[int]] = mapped_column(Integer)
    gender: Mapped[Optional[str]] = mapped_column(String(50))
    education: Mapped[Optional[str]] = mapped_column(String(100))
    annual_income: Mapped[float] = mapped_column(Float)
    employment_years: Mapped[Optional[int]] = mapped_column(Integer)
    home_ownership: Mapped[Optional[str]] = mapped_column(String(50))
    credit_score: Mapped[Optional[int]] = mapped_column(Integer)
    credit_history_years: Mapped[Optional[float]] = mapped_column(Float)
    previous_defaults: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    applications: Mapped[list["Application"]] = relationship(back_populates="applicant")


APPLICATION_STATUSES = ("submitted", "stopped_incomplete", "in_review", "approved", "denied",
                        "pending_applicant")


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (_in("status", APPLICATION_STATUSES),)

    id: Mapped[int] = mapped_column(primary_key=True)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("applicants.id"))
    loan_amount: Mapped[float] = mapped_column(Float)
    loan_intent: Mapped[Optional[str]] = mapped_column(String(50))
    interest_rate: Mapped[Optional[float]] = mapped_column(Float)
    term_months: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="submitted")
    current_step: Mapped[Optional[str]] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    applicant: Mapped[Applicant] = relationship(back_populates="applications")
    documents: Mapped[list["Document"]] = relationship(back_populates="application")
    calculations: Mapped[list["Calculation"]] = relationship(back_populates="application")
    flags: Mapped[list["Flag"]] = relationship(back_populates="application")
    risk_scores: Mapped[list["RiskScore"]] = relationship(back_populates="application")
    recommendations: Mapped[list["Recommendation"]] = relationship(back_populates="application")
    officer_reviews: Mapped[list["OfficerReview"]] = relationship(back_populates="application")
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="application")


class Document(Base):
    """File location only, not the file itself. The SHA-256 proves later that the file on disk
    is the one that was reviewed."""
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    doc_type: Mapped[str] = mapped_column(String(50))
    storage_path: Mapped[str] = mapped_column(String(500))
    original_filename: Mapped[Optional[str]] = mapped_column(String(300))
    mime_type: Mapped[Optional[str]] = mapped_column(String(100))
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    sha256: Mapped[Optional[str]] = mapped_column(String(64))
    page_count: Mapped[Optional[int]] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="documents")
    extracted_fields: Mapped[list["ExtractedField"]] = relationship(back_populates="document")


class ExtractedField(Base):
    """Document Reader output, plus fields computed by code from it (e.g. annual income)."""
    __tablename__ = "extracted_fields"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    field_name: Mapped[str] = mapped_column(String(100))
    field_value: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    source_page: Mapped[Optional[int]] = mapped_column(Integer)
    verified_by_human: Mapped[bool] = mapped_column(Boolean, default=False)
    extraction_model: Mapped[Optional[str]] = mapped_column(String(100))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)

    document: Mapped[Document] = relationship(back_populates="extracted_fields")


class Calculation(Base):
    __tablename__ = "calculations"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    metric: Mapped[str] = mapped_column(String(100))
    value: Mapped[Optional[float]] = mapped_column(Float)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    code_used: Mapped[Optional[str]] = mapped_column(Text)
    assumptions: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="calculations")


class Flag(Base):
    __tablename__ = "flags"
    __table_args__ = (_in("source", ("rule", "pattern")),
                      _in("severity", ("info", "warning", "critical")))

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    code: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(20))
    severity: Mapped[str] = mapped_column(String(20))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="flags")


class RiskScore(Base):
    __tablename__ = "risk_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    model_version: Mapped[str] = mapped_column(String(100))
    default_probability: Mapped[float] = mapped_column(Float)
    contributions: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="risk_scores")


class Recommendation(Base):
    """A suggestion for the officer. Never a final decision."""
    __tablename__ = "recommendations"
    __table_args__ = (_in("recommendation", ("approve", "deny", "refer")),)

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    recommendation: Mapped[str] = mapped_column(String(20))
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    passed_validation: Mapped[Optional[bool]] = mapped_column(Boolean)
    marked_for_additional_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    explanation: Mapped[Optional[str]] = mapped_column(Text)  # the Coordinator's text (phase 2)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="recommendations")


class OfficerReview(Base):
    """The only place a final decision is recorded."""
    __tablename__ = "officer_reviews"
    __table_args__ = (_in("decision", ("approved", "denied", "request_info")),)

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    officer_name: Mapped[str] = mapped_column(String(200))
    decision: Mapped[str] = mapped_column(String(20))
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="officer_reviews")


class AuditLog(Base):
    """Append-only. Rows are never updated or deleted."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[Optional[int]] = mapped_column(ForeignKey("applications.id"))
    step: Mapped[str] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(200))  # code, model, tool, or human:<name>
    action: Mapped[str] = mapped_column(String(200))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    application: Mapped[Optional[Application]] = relationship(back_populates="audit_logs")


@event.listens_for(AuditLog, "before_update")
@event.listens_for(AuditLog, "before_delete")
def _audit_log_is_append_only(mapper, connection, target):
    raise PermissionError("audit_log rows are append-only and cannot be updated or deleted")


def _resolve_url(url: str) -> str:
    """Make a relative SQLite path relative to the project root, not the current folder."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
        path = Path(parsed.database)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        parsed = parsed.set(database=str(path.resolve()))
    return parsed.render_as_string(hide_password=False)


def _upgrade_document_tables(engine: Engine) -> None:
    """Phase 2 added columns to documents and extracted_fields. SQLite can't add them in place
    cleanly, so the two tables are recreated, but only while both are empty. Rows in any other
    table are never touched."""
    insp = inspect(engine)
    have = {c["name"] for c in insp.get_columns("documents")}
    have_fields = {c["name"] for c in insp.get_columns("extracted_fields")}
    want = {c.name for c in Document.__table__.columns}
    want_fields = {c.name for c in ExtractedField.__table__.columns}
    if want <= have and want_fields <= have_fields:
        return
    with engine.begin() as conn:
        counts = {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                  for t in ("documents", "extracted_fields")}
        if any(counts.values()):
            raise RuntimeError(
                f"documents/extracted_fields need new columns but hold rows {counts}. "
                "They are only recreated when empty; migrate them by hand.")
        conn.execute(text("DROP TABLE extracted_fields"))
        conn.execute(text("DROP TABLE documents"))
    Base.metadata.create_all(engine, tables=[Document.__table__, ExtractedField.__table__])


def _add_missing_columns(engine: Engine) -> None:
    """Columns added after a table was created. ADD COLUMN keeps every existing row as it is
    (the new column is NULL), so it is safe on a table that holds data."""
    added = {"recommendations": ["explanation"]}
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, columns in added.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name in columns:
                if name not in have:
                    col = Base.metadata.tables[table].c[name]
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} "
                                      f"{col.type.compile(engine.dialect)}"))


def get_engine() -> Engine:
    load_dotenv(PROJECT_ROOT / ".env")
    url = _resolve_url(os.getenv("DATABASE_URL") or DEFAULT_URL)
    is_sqlite = url.startswith("sqlite")

    engine = create_engine(url, connect_args={"check_same_thread": False} if is_sqlite else {})

    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    Base.metadata.create_all(engine)
    _upgrade_document_tables(engine)
    _add_missing_columns(engine)
    if is_sqlite:
        # The ORM guard above can be bypassed with raw SQL; these triggers cannot.
        with engine.begin() as conn:
            for op in ("UPDATE", "DELETE"):
                conn.execute(text(
                    f"CREATE TRIGGER IF NOT EXISTS audit_log_no_{op.lower()} "
                    f"BEFORE {op} ON audit_log "
                    f"BEGIN SELECT RAISE(ABORT, 'audit_log rows are append-only'); END"))
    return engine


def load_historical(engine: Engine, clean: bool = True) -> pd.DataFrame:
    """Read historical_loans. With clean=True, keep only rows with no previous default."""
    help_msg = "Run `python load_data.py` from the project folder to load the dataset."
    if not inspect(engine).has_table("historical_loans"):
        raise RuntimeError(f"The historical_loans table is missing. {help_msg}")

    query = "SELECT * FROM historical_loans"
    if clean:
        query += " WHERE LOWER(TRIM(previous_loan_defaults_on_file)) = 'no'"
    with engine.connect() as conn:
        df = pd.read_sql(text(query), conn)

    if df.empty:
        raise RuntimeError(f"The historical_loans table is empty. {help_msg}")
    return df


def log(session: Session, step: str, actor: str, action: str,
        application_id: Optional[int] = None, **details) -> AuditLog:
    """Add an audit_log row to the session. The caller commits."""
    entry = AuditLog(application_id=application_id, step=step, actor=actor, action=action,
                     details=details)
    session.add(entry)
    return entry
