"""Persistence model for CDN Manager.

Metric and session tables are plain relational tables so the application runs
on SQLite for a local preview and on PostgreSQL 16 in a real install. At estate
scale the metric and viewing-session tables are the ones the telemetry contract
places on TimescaleDB hypertables; the columns are the same.
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class UtcDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return as_utc(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return as_utc(value)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    salt: Mapped[str] = mapped_column(String(128))
    profile_version: Mapped[int] = mapped_column(Integer, default=1)
    # field name -> required | optional | not_collected | observed
    profile_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    retention_days: Mapped[int] = mapped_column(Integer, default=90)
    track_b_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    truncate_client_ip: Mapped[bool] = mapped_column(Boolean, default=False)
    signed_urls: Mapped[bool] = mapped_column(Boolean, default=True)
    ip_allowlist: Mapped[str] = mapped_column(Text, default="")
    api_key_sha256: Mapped[str] = mapped_column(String(64))
    active_subscriber_base: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subscriber_base_as_of: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(32), index=True)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    subscriber_data_read: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class LoginSession(Base):
    __tablename__ = "login_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    tier: Mapped[str] = mapped_column(String(16), index=True)  # edge | mid | origin
    size_class: Mapped[str] = mapped_column(String(8), default="M")
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    region: Mapped[str] = mapped_column(String(64), default="")
    city: Mapped[str] = mapped_column(String(64), default="")
    asn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_url: Mapped[str] = mapped_column(String(256), default="")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    nic_capacity_mbps: Mapped[int] = mapped_column(Integer, default=10000)
    desired_config_hash: Mapped[str] = mapped_column(String(64), default="")
    signing_secret: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    origin_role: Mapped[str] = mapped_column(String(16), default="")
    paired_node_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    simulate: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="offline", index=True)
    agent_version: Mapped[str] = mapped_column(String(32), default="")
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    last_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    counter_epoch: Mapped[str] = mapped_column(String(32), default="")
    counters: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_ip: Mapped[str] = mapped_column(String(64), default="")
    public_ipv4: Mapped[str] = mapped_column(String(64), default="")
    nginx_running: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    stream_uuid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    code: Mapped[str] = mapped_column(String(64), default="")
    category: Mapped[str] = mapped_column(String(64), default="")
    protocol: Mapped[str] = mapped_column(String(16), default="hls")
    kind: Mapped[str] = mapped_column(String(16), default="live")
    logo_url: Mapped[str] = mapped_column(String(256), default="")
    retired_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class StreamSighting(Base):
    """Traffic for a stream UUID, including UUIDs that have no channel row."""

    __tablename__ = "stream_sightings"

    stream_uuid: Mapped[str] = mapped_column(String(64), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime())
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime())
    requests: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0)
    unmapped_alerted: Mapped[bool] = mapped_column(Boolean, default=False)


class TelemetryBatch(Base):
    __tablename__ = "telemetry_batches"
    __table_args__ = (UniqueConstraint("node_id", "batch_id", name="uq_batch"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    schema_version: Mapped[str] = mapped_column(String(16))
    sent_at: Mapped[datetime] = mapped_column(UtcDateTime())
    accepted_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    backfill: Mapped[bool] = mapped_column(Boolean, default=False)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    session_count: Mapped[int] = mapped_column(Integer, default=0)


class MetricSample(Base):
    __tablename__ = "metric_samples"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    ts: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    interval_s: Mapped[float] = mapped_column(Float)
    counter_epoch: Mapped[str] = mapped_column(String(32), default="")
    cpu_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_used_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    cache_used_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    connections_active: Mapped[int | None] = mapped_column(Integer, nullable=True)
    peak_concurrent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ntp_offset_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    nginx_running: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    d_body_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    d_tx_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    d_upstream_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    d_requests: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    d_5xx: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    d_499: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    d_hit_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    d_miss_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    d_request_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    d_request_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    request_time_bucket: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cold_cache: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class ContentStat(Base):
    __tablename__ = "content_stats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    ts: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    stream_uuid: Mapped[str] = mapped_column(String(64), index=True)
    key_type: Mapped[str] = mapped_column(String(32), default="stream_uuid")
    requests: Mapped[int] = mapped_column(Integer, default=0)
    bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0)
    hit_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    miss_bytes: Mapped[int] = mapped_column(BigInteger, default=0)


class ViewershipStat(Base):
    __tablename__ = "viewership_stats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    ts: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    stream_uuid: Mapped[str] = mapped_column(String(64), index=True)
    distinct_subscribers_est: Mapped[int] = mapped_column(Integer, default=0)
    peak_concurrent: Mapped[int] = mapped_column(Integer, default=0)
    sessions_started: Mapped[int] = mapped_column(Integer, default=0)
    sessions_ended: Mapped[int] = mapped_column(Integer, default=0)


class SequenceGap(Base):
    __tablename__ = "sequence_gaps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    counter_epoch: Mapped[str] = mapped_column(String(32))
    expected_sequence: Mapped[int] = mapped_column(BigInteger)
    got_sequence: Mapped[int] = mapped_column(BigInteger)
    at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class EpochBoundary(Base):
    __tablename__ = "epoch_boundaries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    previous_epoch: Mapped[str] = mapped_column(String(32), default="")
    new_epoch: Mapped[str] = mapped_column(String(32))
    at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    agent_reported: Mapped[bool] = mapped_column(Boolean, default=False)


class DataQualityEvent(Base):
    __tablename__ = "data_quality_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    kind: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class UsageBucket(Base):
    __tablename__ = "usage_buckets"
    __table_args__ = (UniqueConstraint("node_id", "bucket_start", name="uq_usage_bucket"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    bucket_start: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    body_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    requests: Mapped[int] = mapped_column(BigInteger, default=0)
    gap: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_hold: Mapped[bool] = mapped_column(Boolean, default=False)


class UsagePeriod(Base):
    __tablename__ = "usage_periods"
    __table_args__ = (UniqueConstraint("tenant_id", "period_start", name="uq_usage_period"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    period_start: Mapped[datetime] = mapped_column(UtcDateTime())
    period_end: Mapped[datetime] = mapped_column(UtcDateTime())
    body_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    gap: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="finalised")
    finalized_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class UsageAdjustment(Base):
    __tablename__ = "usage_adjustments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    period_id: Mapped[str] = mapped_column(ForeignKey("usage_periods.id"), index=True)
    body_bytes_delta: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class ViewingSession(Base):
    __tablename__ = "viewing_sessions"
    __table_args__ = (UniqueConstraint("node_id", "session_id", name="uq_viewing_session"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    session_id: Mapped[str] = mapped_column(String(64))
    session_handle: Mapped[str] = mapped_column(String(64), index=True)
    subscriber_pseudonym: Mapped[str] = mapped_column(String(32), default="", index=True)
    stream_uuid: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime())
    ended_at: Mapped[datetime] = mapped_column(UtcDateTime())
    duration_s: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    playlist_requests: Mapped[int] = mapped_column(Integer, default=0)
    segment_requests: Mapped[int] = mapped_column(Integer, default=0)
    bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_attribution: Mapped[str] = mapped_column(String(16), default="exact")
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    abort_count: Mapped[int] = mapped_column(Integer, default=0)
    client_ip: Mapped[str] = mapped_column(String(64), default="")
    ua_class: Mapped[str] = mapped_column(String(32), default="")
    closed_reason: Mapped[str] = mapped_column(String(32), default="")
    continuation_of: Mapped[str] = mapped_column(String(64), default="")
    handle_redacted: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class SessionRegistration(Base):
    __tablename__ = "session_registrations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    session_handle: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    subscriber_code: Mapped[str] = mapped_column(String(64), index=True)
    opened_at: Mapped[datetime] = mapped_column(UtcDateTime())
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    stored_attributes: Mapped[list] = mapped_column(JSON, default=list)
    dropped_attributes: Mapped[list] = mapped_column(JSON, default=list)
    profile_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class SubscriberResolution(Base):
    __tablename__ = "subscriber_resolutions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    session_row_id: Mapped[str] = mapped_column(String(36))
    session_handle: Mapped[str] = mapped_column(String(64))
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (UniqueConstraint("node_id", "event_id", name="uq_agent_event"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    event_id: Mapped[str] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(UtcDateTime())
    type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str | None] = mapped_column(ForeignKey("nodes.id"), nullable=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    opened_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    acked_by: Mapped[str] = mapped_column(String(64), default="")
    acked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class NodeFact(Base):
    __tablename__ = "node_facts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    reported_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    facts: Mapped[dict] = mapped_column(JSON, default=dict)


class SyslogMessage(Base):
    __tablename__ = "syslog_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str | None] = mapped_column(ForeignKey("nodes.id"), nullable=True, index=True)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)


class EpgChannelMap(Base):
    __tablename__ = "epg_channel_map"

    epg_channel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stream_uuid: Mapped[str] = mapped_column(String(64), index=True)


class EpgProgramme(Base):
    __tablename__ = "epg_programmes"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "stream_uuid",
            "programme_ext_id",
            "scheduled_start",
            name="uq_epg_programme",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    stream_uuid: Mapped[str] = mapped_column(String(64), index=True)
    programme_ext_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(256))
    subtitle: Mapped[str] = mapped_column(String(256), default="")
    genre: Mapped[str] = mapped_column(String(64), default="")
    scheduled_start: Mapped[datetime] = mapped_column(UtcDateTime())
    scheduled_end: Mapped[datetime] = mapped_column(UtcDateTime())
    actual_start: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    actual_end: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="manual")
    ingested_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class Webhook(Base):
    __tablename__ = "webhooks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    url: Mapped[str] = mapped_column(String(512))
    secret: Mapped[str] = mapped_column(String(128))
    event_types: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SteeringRule(Base):
    """Send a subscriber, prefix, region, or network to an edge."""

    __tablename__ = "steering_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    match_type: Mapped[str] = mapped_column(String(32))  # ip_cidr | region | asn | subscriber_prefix
    match_value: Mapped[str] = mapped_column(String(128))
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class PrepositionJob(Base):
    __tablename__ = "preposition_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    stream_uuid: Mapped[str] = mapped_column(String(64), index=True)
    requested_by: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="requested")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class PurgeRequest(Base):
    __tablename__ = "purge_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(String(16))  # file | channel | group | cache
    target: Mapped[str] = mapped_column(String(256), default="")
    node_id: Mapped[str | None] = mapped_column(ForeignKey("nodes.id"), nullable=True, index=True)
    requested_by: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="requested")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    webhook_id: Mapped[str] = mapped_column(ForeignKey("webhooks.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
