from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    site_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    brand_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    workspace_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    stages: Mapped[list[ProjectStage]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectStage.position",
        lazy="selectin",
    )

    jobs: Mapped[list[WorkflowJob]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="WorkflowJob.created_at",
        lazy="selectin",
    )


class ProjectStage(Base):
    __tablename__ = "project_stages"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id", ondelete="CASCADE"), primary_key=True
    )
    stage_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    completed_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tasks: Mapped[int] = mapped_column(Integer, nullable=False)
    active_task: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    project: Mapped[Project] = relationship(back_populates="stages")


class WorkflowJob(Base):
    __tablename__ = "workflow_jobs"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_workflow_job_idempotency"),
    )

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_key: Mapped[str] = mapped_column(String(64), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    simulation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    project: Mapped[Project] = relationship(back_populates="jobs")
    events: Mapped[list[WorkflowJobEvent]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="WorkflowJobEvent.sequence",
        lazy="selectin",
    )


class WorkflowJobEvent(Base):
    __tablename__ = "workflow_job_events"
    __table_args__ = (
        UniqueConstraint("job_id", "sequence", name="uq_workflow_job_event_sequence"),
    )

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_jobs.job_id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    job: Mapped[WorkflowJob] = relationship(back_populates="events")


class SiteProfile(Base):
    __tablename__ = "site_profiles"
    site_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    profile_json: Mapped[str] = mapped_column(Text, nullable=False)
    rules_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="ready")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class CrawlRequest(Base):
    __tablename__ = "crawl_requests"
    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class CrawlResult(Base):
    __tablename__ = "crawl_results"
    result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("crawl_requests.request_id", ondelete="CASCADE"), index=True)
    classification: Mapped[str] = mapped_column(String(64), nullable=False)
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class CatalogEntry(Base):
    __tablename__ = "catalog_entries"
    __table_args__ = (UniqueConstraint("project_id", "record_id", name="uq_catalog_project_record"),)
    catalog_entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), index=True)
    record_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    identity_key: Mapped[str] = mapped_column(String(512), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_asset_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    catalog_lock_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="candidate")
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class SourceAsset(Base):
    __tablename__ = "source_assets"
    __table_args__ = (UniqueConstraint("project_id", "relative_path", name="uq_source_project_path"),)
    asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), index=True)
    record_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ProviderTask(Base):
    __tablename__ = "provider_tasks"
    __table_args__ = (UniqueConstraint("job_id", "idempotency_key", name="uq_provider_job_idempotency"),)
    provider_task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("workflow_jobs.job_id", ondelete="CASCADE"), index=True)
    record_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="submitted")
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ModelRun(Base):
    __tablename__ = "model_runs"
    model_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("workflow_jobs.job_id", ondelete="CASCADE"), index=True)
    record_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    raw_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processed_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="pending")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class DeliveryBatch(Base):
    __tablename__ = "delivery_batches"
    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), index=True)
    batch_number: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="ready_for_upload")
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class DeliveryItem(Base):
    __tablename__ = "delivery_items"
    __table_args__ = (UniqueConstraint("batch_id", "record_id", name="uq_delivery_batch_record"),)
    delivery_item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("delivery_batches.batch_id", ondelete="CASCADE"), index=True)
    record_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    model_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    qa_status: Mapped[str] = mapped_column(String(64), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class SiteRoundtrip(Base):
    __tablename__ = "site_roundtrips"
    roundtrip_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("delivery_batches.batch_id", ondelete="CASCADE"), index=True)
    external_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="waiting_external_upload")
    imported_manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class QAResult(Base):
    __tablename__ = "qa_results"
    qa_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), index=True)
    record_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    report_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Incident(Base):
    __tablename__ = "incidents"
    incident_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.project_id", ondelete="SET NULL"), nullable=True, index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_jobs.job_id", ondelete="SET NULL"), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="P2")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Website Native Agentless control-plane entities. These tables hold durable
# site-analysis evidence, orchestration state, and receipts. Skills 8.8.5 is a
# frozen reference package and is not a runtime dependency of Website.
class SiteRegistryRecord(Base):
    __tablename__ = "site_registry"

    site_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="UNKNOWN")
    source_health: Mapped[str] = mapped_column(String(64), nullable=False, default="UNKNOWN")
    acquisition_mode: Mapped[str] = mapped_column(String(64), nullable=False, default="UNKNOWN")
    brand_policy: Mapped[str] = mapped_column(String(64), nullable=False, default="REVIEW")
    media_policy: Mapped[str] = mapped_column(String(64), nullable=False, default="EVIDENCE_ONLY")
    config_complexity: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    watermark_risk: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    profile_version: Mapped[str] = mapped_column(String(64), nullable=False, default="unverified")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DRAFT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class SiteTaxonomySnapshot(Base):
    __tablename__ = "site_taxonomy_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    native_categories_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    canonical_categories_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class SiteCategory(Base):
    __tablename__ = "site_categories"
    __table_args__ = (UniqueConstraint("site_key", "path", name="uq_site_category_path"),)

    category_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    native_name: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    count_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    count_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN")
    reported_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confidence: Mapped[float] = mapped_column(nullable=False, default=0.0)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    parent_category_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="CATEGORY")


class SiteScanRun(Base):
    __tablename__ = "site_scan_runs"

    scan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED")
    live: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    taxonomy_level: Mapped[str] = mapped_column(String(16), nullable=False, default="L0")
    brain_status: Mapped[str] = mapped_column(String(64), nullable=False, default="NOT_NEEDED")
    provider_posts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    receipt_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    command_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    browser_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resume_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProductionJob(Base):
    __tablename__ = "production_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    site_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    target_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="EXACT_N")
    target_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scope: Mapped[str] = mapped_column(String(48), nullable=False, default="NEW_ONLY")
    category_allocation: Mapped[str] = mapped_column(String(48), nullable=False, default="TOTAL_ACROSS_SELECTED")
    allocation_strategy: Mapped[str] = mapped_column(String(48), nullable=False, default="SEQUENTIAL")
    spillover: Mapped[str] = mapped_column(String(32), nullable=False, default="ASK")
    status: Mapped[str] = mapped_column(String(48), nullable=False, default="DRAFT")
    current_stage: Mapped[str] = mapped_column(String(64), nullable=False, default="URL_AND_GOAL")
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reported_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unique_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shortage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="OFF")
    provider_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_safety: Mapped[str] = mapped_column(String(48), nullable=False, default="NOT_CHECKED")
    policy_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_pool_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    ready_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_qualification_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ProductionJobEvent(Base):
    __tablename__ = "production_job_events"
    __table_args__ = (UniqueConstraint("job_id", "sequence", name="uq_production_job_event_sequence"),)

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("production_jobs.job_id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_event_id: Mapped[str | None] = mapped_column(String(160), nullable=True, unique=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False, default="control-event.v1")
    source_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    items_done: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ReviewQueueItem(Base):
    __tablename__ = "review_queue_items"

    review_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("production_jobs.job_id", ondelete="SET NULL"), nullable=True, index=True)
    record_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="P2")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="OPEN")
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ProviderSafetyCheck(Base):
    __tablename__ = "provider_safety_checks"

    check_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("production_jobs.job_id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_idempotency_safe: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    submission_unknown_guard: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PRODUCTION_BLOCKED")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    qualification_receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorization_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ProductionRegistryEntry(Base):
    """Permanent product identity and completed-model history for Website production."""

    __tablename__ = "production_registry_entries"
    __table_args__ = (
        UniqueConstraint("site_key", "identity_key", name="uq_production_registry_identity"),
    )

    registry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    identity_key: Mapped[str] = mapped_column(String(768), nullable=False)
    source_product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    governed_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    source_brand: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    category_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    category_group: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    completed_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    image_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_glb_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    raw_glb_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(48), nullable=False, default="DISCOVERED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ProductionProviderTask(Base):
    """Durable paid-provider submission ledger; one idempotency key means one create POST."""

    __tablename__ = "production_provider_tasks"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_production_provider_idempotency"),
        UniqueConstraint("job_id", "candidate_id", name="uq_production_provider_candidate"),
    )

    ledger_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    record_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    model_input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="PREPARED")
    checkpoint_state: Mapped[str] = mapped_column(String(64), nullable=False, default="PREPARED")
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    poll_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class BrowserSession(Base):
    """Scoped persistent headed-browser session used for compliant L2 handoff/resume."""

    __tablename__ = "browser_sessions"

    browser_session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    scan_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_data_dir: Mapped[str] = mapped_column(Text, nullable=False)
    current_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(48), nullable=False, default="READY")
    challenge_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    challenge_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class AIAssistantDecisionReceipt(Base):
    __tablename__ = "ai_decision_receipts"

    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("production_jobs.job_id", ondelete="SET NULL"), nullable=True, index=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    temperature: Mapped[str] = mapped_column(String(16), nullable=False, default="0")
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    validation_result: Mapped[str] = mapped_column(String(32), nullable=False, default="NOT_RUN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ControlAuditLog(Base):
    __tablename__ = "control_audit_logs"

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False, default="RECORDED")
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ProductionRun(Base):
    __tablename__ = "production_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("production_jobs.job_id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING")
    stage: Mapped[str] = mapped_column(String(64), nullable=False, default="launching")
    progress_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    items_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    workspace: Mapped[str | None] = mapped_column(Text, nullable=True)
    stdout_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    command_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    events_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    contract_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    queue_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_key: Mapped[str] = mapped_column(String(64), nullable=False, default="production-v1")
    launch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SiteEntryURL(Base):
    __tablename__ = "site_entry_urls"
    __table_args__ = (UniqueConstraint("site_key", "url", name="uq_site_entry_url"),)

    entry_url_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_key: Mapped[str] = mapped_column(ForeignKey("site_registry.site_key", ondelete="CASCADE"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_taxonomy_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_status: Mapped[str] = mapped_column(String(48), nullable=False, default="RECEIVED")


class RuntimeEvent(Base):
    __tablename__ = "runtime_events"
    __table_args__ = (
        UniqueConstraint("job_id", "runtime_event_id", name="uq_runtime_event_identity"),
        UniqueConstraint("job_id", "sequence", name="uq_runtime_event_sequence"),
    )

    runtime_event_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("production_jobs.job_id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False, default="workflow-event.v2")
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    items_done: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ProductionArtifact(Base):
    __tablename__ = "production_artifacts"
    __table_args__ = (UniqueConstraint("job_id", "artifact_type", "relative_path", name="uq_job_artifact_path"),)

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("production_jobs.job_id", ondelete="CASCADE"), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("production_runs.run_id", ondelete="SET NULL"), nullable=True, index=True)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manifest_schema: Mapped[str | None] = mapped_column(String(128), nullable=True)
    receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
