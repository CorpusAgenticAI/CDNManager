"""Catalogue, sessions, billing, audience, and dashboard figures.

Tenant scoping for subscriber data lives in this module. The isolation tests
fail if a tenant filter is removed from session resolution.
"""

from __future__ import annotations

import csv
import io
import hmac
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Alert,
    AuditLog,
    Channel,
    EpgChannelMap,
    EpgProgramme,
    MetricSample,
    Node,
    SessionRegistration,
    SteeringRule,
    StreamSighting,
    SubscriberResolution,
    SyslogMessage,
    Tenant,
    UsageAdjustment,
    UsageBucket,
    UsagePeriod,
    User,
    ViewershipStat,
    ViewingSession,
    Webhook,
    WebhookDelivery,
    utcnow,
)
from app.security import pseudonym, sha256_hex, truncate_ip

PROFILE_MODES = {"required", "optional", "not_collected", "observed"}


def _choice(value, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def audit(db: AsyncSession, actor: str, action: str, detail: dict | None = None) -> None:
    db.add(AuditLog(actor=actor, action=action, detail=detail or {}))


def apply_profile(tenant: Tenant, attributes: dict | None) -> tuple[dict, list[str], list[str], list[str]]:
    """Return stored attributes, stored names, dropped names, and missing required names."""
    incoming = attributes or {}
    profile = tenant.profile_fields or {}
    stored: dict = {}
    stored_names: list[str] = []
    dropped: list[str] = []
    missing: list[str] = []
    for name, mode in profile.items():
        if mode == "required" and name != "subscriber_code" and not incoming.get(name):
            missing.append(name)
        elif mode == "optional" and incoming.get(name):
            stored[name] = incoming[name]
            stored_names.append(name)
        elif mode == "required" and name != "subscriber_code" and incoming.get(name):
            stored[name] = incoming[name]
            stored_names.append(name)
        elif mode in {"not_collected", "observed"} and name in incoming:
            dropped.append(name)
    for name in incoming:
        if name not in profile or profile.get(name) not in {"required", "optional"}:
            if name not in dropped:
                dropped.append(name)
            stored.pop(name, None)
            if name in stored_names:
                stored_names.remove(name)
    if tenant.truncate_client_ip and "client_ip" in stored:
        stored["client_ip"] = truncate_ip(str(stored["client_ip"]))
    return stored, stored_names, dropped, missing


async def register_session(db: AsyncSession, tenant: Tenant, body: dict) -> tuple[int, dict]:
    handle = str(body.get("session_handle") or "")
    code = str(body.get("subscriber_code") or "")
    if body.get("tenant_code") != tenant.code:
        return 403, {"error": "tenant_code does not match the API key"}
    parts = handle.split(".")
    if len(parts) != 3 or parts[0] != "1" or not code:
        return 422, {"error": "session_handle must be 1.<pseudonym>.<nonce> and subscriber_code is required"}
    expected = pseudonym(tenant.salt, code)
    if not hmac.compare_digest(parts[1], expected):
        return 422, {"error": "pseudonym does not match the tenant salt"}
    try:
        opened = datetime.fromisoformat(str(body["opened_at"]).replace("Z", "+00:00"))
        expires = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00"))
    except Exception:
        return 422, {"error": "opened_at and expires_at must be RFC3339"}
    stored, stored_names, dropped, missing = apply_profile(tenant, body.get("attributes") or {})
    if "subscriber_code" in (tenant.profile_fields or {}) and tenant.profile_fields.get("subscriber_code") == "required" and not code:
        missing.append("subscriber_code")
    if missing:
        return 422, {"error": "required profile attributes are missing", "missing": missing}

    existing = await db.execute(select(SessionRegistration).where(SessionRegistration.session_handle == handle))
    current = existing.scalar_one_or_none()
    result = {
        "session_handle": handle,
        "stored_attributes": stored_names,
        "dropped_attributes": dropped,
        "profile_version": tenant.profile_version,
    }
    if current is not None:
        if current.subscriber_code != code or current.tenant_id != tenant.id:
            return 409, {"error": "session_handle is already registered to a different subscriber"}
        return 200, {
            "session_handle": handle,
            "stored_attributes": current.stored_attributes,
            "dropped_attributes": current.dropped_attributes,
            "profile_version": current.profile_version,
        }
    db.add(
        SessionRegistration(
            tenant_id=tenant.id,
            session_handle=handle,
            subscriber_code=code,
            opened_at=aware(opened),
            expires_at=aware(expires),
            attributes=stored,
            stored_attributes=stored_names,
            dropped_attributes=dropped,
            profile_version=tenant.profile_version,
        )
    )
    await db.flush()
    return 201, result


async def tenant_by_api_key(db: AsyncSession, token: str) -> Tenant | None:
    digest = sha256_hex(token)
    result = await db.execute(select(Tenant).where(Tenant.api_key_sha256 == digest, Tenant.active.is_(True)))
    return result.scalar_one_or_none()


async def import_channels(db: AsyncSession, body: dict, actor: str) -> dict:
    rows = body.get("channels")
    if not isinstance(rows, list):
        return {"error": "channels array is required"}
    created = 0
    updated = 0
    rejected: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("stream_uuid") or not row.get("name"):
            rejected.append({"row": row, "error": "stream_uuid and name are required"})
            continue
        stream_uuid = str(row["stream_uuid"])
        found = await db.execute(select(Channel).where(Channel.stream_uuid == stream_uuid))
        channel = found.scalar_one_or_none()
        if channel is None:
            db.add(
                Channel(
                    stream_uuid=stream_uuid,
                    name=str(row["name"]),
                    code=str(row.get("code") or ""),
                    category=str(row.get("category") or ""),
                    protocol=_choice(row.get("protocol"), {"hls", "dash", "hls_dash"}, "hls"),
                    kind=_choice(row.get("kind"), {"live", "vod"}, "live"),
                    logo_url=str(row.get("logo_url") or ""),
                )
            )
            created += 1
            continue
        if channel.retired_at is not None and (
            channel.name != row["name"] or (row.get("code") and channel.code != row.get("code"))
        ):
            rejected.append({"stream_uuid": stream_uuid, "error": "retired stream UUID cannot be reassigned"})
            continue
        channel.name = str(row["name"])
        channel.code = str(row.get("code") or channel.code)
        channel.category = str(row.get("category") or channel.category)
        if row.get("protocol"):
            channel.protocol = _choice(row.get("protocol"), {"hls", "dash", "hls_dash"}, channel.protocol)
        if row.get("kind"):
            channel.kind = _choice(row.get("kind"), {"live", "vod"}, channel.kind)
        if row.get("logo_url"):
            channel.logo_url = str(row["logo_url"])
        updated += 1
    await audit(db, actor, "channels.import", {"created": created, "updated": updated, "rejected": len(rejected)})
    return {"created": created, "updated": updated, "rejected": rejected}


def parse_channel_csv(text: str) -> dict:
    reader = csv.DictReader(io.StringIO(text))
    return {"channels": list(reader)}


async def edge_map(db: AsyncSession, tenant_code: str) -> tuple[int, dict]:
    result = await db.execute(select(Tenant).where(Tenant.code == tenant_code, Tenant.active.is_(True)))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        return 404, {"error": "unknown tenant"}
    nodes = await db.execute(
        select(Node).where(Node.tenant_id == tenant.id, Node.tier == "edge", Node.enabled.is_(True))
    )
    edges = []
    for node in nodes.scalars():
        health = "healthy"
        if node.status == "offline":
            health = "unhealthy"
        else:
            open_alerts = await db.execute(
                select(func.count())
                .select_from(Alert)
                .where(
                    Alert.node_id == node.id,
                    Alert.closed_at.is_(None),
                    Alert.severity.in_(("major", "critical")),
                )
            )
            if open_alerts.scalar_one():
                health = "degraded"
        edges.append(
            {
                "node_id": node.id,
                "name": node.name,
                "base_url": node.base_url,
                "priority": node.priority,
                "health": health,
                "region": node.region,
                "city": node.city,
            }
        )
    rank = {"healthy": 0, "degraded": 1, "unhealthy": 2}
    edges.sort(key=lambda item: (rank[item["health"]], item["priority"], item["name"]))
    return 200, {
        "tenant_code": tenant.code,
        "ttl_seconds": 60,
        "generated_at": utcnow().isoformat(),
        "edges": edges,
    }


def scoped_tenant(user: User) -> str | None:
    if user.role in {"tenant_user", "support", "billing"}:
        return user.tenant_id
    return None


async def resolve_session(db: AsyncSession, user: User, session_row_id: str) -> tuple[int, dict]:
    row = await db.get(ViewingSession, session_row_id)
    if row is None:
        return 404, {"error": "session not found"}
    scope = scoped_tenant(user)
    if scope is not None and row.tenant_id != scope:
        return 404, {"error": "session not found"}
    if not user.subscriber_data_read:
        return 403, {"error": "subscriber_data:read is required"}
    registration = await db.execute(
        select(SessionRegistration).where(SessionRegistration.session_handle == row.session_handle)
    )
    directory = registration.scalar_one_or_none()
    if directory is not None and scope is not None and directory.tenant_id != scope:
        directory = None
    db.add(
        SubscriberResolution(
            actor=user.username,
            session_row_id=row.id,
            session_handle=row.session_handle,
            tenant_id=row.tenant_id,
        )
    )
    await audit(db, user.username, "subscriber.resolve", {"session_id": row.session_id, "tenant_id": row.tenant_id})
    if directory is None or row.handle_redacted:
        return 200, {"resolved": None, "reason": "unresolved"}
    tenant = await db.get(Tenant, directory.tenant_id)
    payload = {"subscriber_code": directory.subscriber_code, "tenant": tenant.code if tenant else ""}
    payload.update(directory.attributes or {})
    return 200, {"resolved": payload}


def overlap_seconds(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> float:
    start_a, end_a, start_b, end_b = map(aware, (start_a, end_a, start_b, end_b))
    return max(0.0, (min(end_a, end_b) - max(start_a, start_b)).total_seconds())


async def programme_audience(db: AsyncSession, programme: EpgProgramme) -> dict:
    start = programme.actual_start or programme.scheduled_start
    end = programme.actual_end or programme.scheduled_end
    confidence = "as_run" if programme.actual_start and programme.actual_end else "as_published"
    duration = max((aware(end) - aware(start)).total_seconds(), 1)
    result = await db.execute(select(ViewingSession).where(ViewingSession.stream_uuid == programme.stream_uuid))
    sessions = list(result.scalars())
    if programme.tenant_id:
        sessions = [row for row in sessions if row.tenant_id == programme.tenant_id]
    total_overlap = 0.0
    by_person: dict[str, float] = {}
    minute_buckets: dict[datetime, int] = {}
    for row in sessions:
        seconds = overlap_seconds(row.started_at, row.ended_at, start, end)
        if seconds <= 0:
            continue
        total_overlap += seconds
        person = row.subscriber_pseudonym or row.session_handle
        by_person[person] = by_person.get(person, 0) + seconds
        cursor = aware(max(aware(row.started_at), aware(start))).replace(second=0, microsecond=0)
        stop = aware(min(aware(row.ended_at), aware(end)))
        while cursor < stop:
            minute_buckets[cursor] = minute_buckets.get(cursor, 0) + 1
            cursor += timedelta(minutes=1)
    reach_60 = sum(1 for value in by_person.values() if value >= 60)
    reach_300 = sum(1 for value in by_person.values() if value >= 300)
    aa = total_overlap / duration
    atv = (total_overlap / reach_60) if reach_60 else 0
    peak = max(minute_buckets.values()) if minute_buckets else 0
    return {
        "programme_id": programme.id,
        "title": programme.title,
        "schedule_confidence": confidence,
        "average_audience": round(aa, 2),
        "reach_60s": reach_60,
        "reach_300s": reach_300,
        "average_time_viewed_s": round(atv, 1),
        "peak_audience": peak,
        "duration_s": int(duration),
    }


async def finalize_periods(db: AsyncSession, now: datetime | None = None) -> list[UsagePeriod]:
    now = now or utcnow()
    cutoff = now - timedelta(minutes=10)
    result = await db.execute(select(UsageBucket).where(UsageBucket.bucket_start < cutoff))
    grouped: dict[tuple[str, datetime], list[UsageBucket]] = {}
    for bucket in result.scalars():
        if not bucket.tenant_id:
            continue
        hour = bucket.bucket_start.replace(minute=0, second=0, microsecond=0)
        if hour + timedelta(hours=1) > cutoff:
            continue
        grouped.setdefault((bucket.tenant_id, hour), []).append(bucket)
    created: list[UsagePeriod] = []
    for (tenant_id, hour), buckets in grouped.items():
        existing = await db.execute(
            select(UsagePeriod).where(UsagePeriod.tenant_id == tenant_id, UsagePeriod.period_start == hour)
        )
        if existing.scalar_one_or_none() is not None:
            continue
        period = UsagePeriod(
            tenant_id=tenant_id,
            period_start=hour,
            period_end=hour + timedelta(hours=1),
            body_bytes=sum(item.body_bytes for item in buckets),
            gap=any(item.gap or item.quality_hold for item in buckets),
        )
        db.add(period)
        created.append(period)
    if created:
        await db.flush()
    return created


async def dispatch_webhooks(db: AsyncSession, event_type: str, payload: dict) -> None:
    result = await db.execute(select(Webhook).where(Webhook.active.is_(True)))
    hooks = [hook for hook in result.scalars() if not hook.event_types or event_type in hook.event_types]
    async with httpx.AsyncClient(timeout=2.0) as client:
        for hook in hooks:
            status = None
            error = ""
            try:
                response = await client.post(
                    hook.url,
                    json={"type": event_type, "data": payload},
                    headers={"X-CDN-Event": event_type, "X-CDN-Secret": hook.secret},
                )
                status = response.status_code
            except Exception as exc:
                error = str(exc)
            db.add(
                WebhookDelivery(
                    webhook_id=hook.id,
                    event_type=event_type,
                    payload=payload,
                    status_code=status,
                    error=error,
                )
            )


async def run_retention(db: AsyncSession) -> dict:
    now = utcnow()
    tenants = await db.execute(select(Tenant))
    expired_directory = 0
    for tenant in tenants.scalars():
        cutoff = now - timedelta(days=tenant.retention_days)
        rows = await db.execute(
            select(SessionRegistration).where(
                SessionRegistration.tenant_id == tenant.id,
                SessionRegistration.created_at < cutoff,
            )
        )
        for row in rows.scalars():
            await db.delete(row)
            expired_directory += 1
    session_cutoff = now - timedelta(days=90)
    redacted = 0
    old_sessions = await db.execute(
        select(ViewingSession).where(
            ViewingSession.ended_at < session_cutoff,
            ViewingSession.handle_redacted.is_(False),
        )
    )
    for row in old_sessions.scalars():
        row.session_handle = ""
        row.client_ip = ""
        row.handle_redacted = True
        redacted += 1
    log_cutoff = now - timedelta(days=14)
    logs = await db.execute(select(SyslogMessage).where(SyslogMessage.received_at < log_cutoff))
    dropped_logs = 0
    for row in logs.scalars():
        await db.delete(row)
        dropped_logs += 1
    return {"directory_expired": expired_directory, "sessions_redacted": redacted, "syslog_dropped": dropped_logs}


def percentile_from_buckets(buckets: dict | None, percentile: float = 0.95) -> float | None:
    if not buckets:
        return None
    order = ["10", "25", "50", "100", "250", "500", "1000", "2500", "5000", "10000", "+Inf"]
    total = buckets.get("+Inf")
    if not isinstance(total, int) or total <= 0:
        total = max((value for value in buckets.values() if isinstance(value, int)), default=0)
    if total <= 0:
        return None
    target = total * percentile
    previous = 0.0
    for key in order:
        count = buckets.get(key)
        if not isinstance(count, int):
            continue
        if count >= target:
            try:
                return float(key)
            except ValueError:
                return previous
        if key != "+Inf":
            previous = float(key)
    return previous or None


async def dashboard_kpis(db: AsyncSession, tenant_id: str | None = None) -> dict:
    since = utcnow() - timedelta(minutes=15)
    node_query = select(Node)
    if tenant_id:
        node_query = node_query.where(Node.tenant_id == tenant_id)
    nodes = list((await db.execute(node_query)).scalars())
    node_ids = [node.id for node in nodes]
    samples: list[MetricSample] = []
    if node_ids:
        sample_rows = await db.execute(select(MetricSample).where(MetricSample.ts >= since, MetricSample.node_id.in_(node_ids)))
        samples = list(sample_rows.scalars())

    def total(attr: str, ids: set[str] | None = None) -> int:
        return sum(
            getattr(row, attr) or 0
            for row in samples
            if getattr(row, attr) is not None and (ids is None or row.node_id in ids)
        )

    edge_ids = {node.id for node in nodes if node.tier == "edge"}
    mid_ids = {node.id for node in nodes if node.tier == "mid"}
    body = total("d_body_bytes", edge_ids)
    upstream = total("d_upstream_bytes", edge_ids)
    hit = total("d_hit_bytes", edge_ids)
    miss = total("d_miss_bytes", edge_ids)
    tx = total("d_tx_bytes", edge_ids)
    origin_fill = total("d_upstream_bytes", mid_ids)
    req_ms = total("d_request_ms", edge_ids)
    req_n = total("d_request_count", edge_ids)
    intervals = [row.interval_s for row in samples if row.node_id in edge_ids and row.interval_s]
    elapsed = sum(intervals) / max(len({row.node_id for row in samples if row.node_id in edge_ids}), 1)
    mbps = (tx * 8 / elapsed / 1_000_000) if elapsed else 0
    chr = (hit / (hit + miss) * 100) if (hit + miss) else None
    saved = body - upstream if body and upstream is not None else None

    enabled_edges = [node for node in nodes if node.tier == "edge" and node.enabled]
    available = [node for node in enabled_edges if node.status == "online" and node.nginx_running]
    latest_by_node: dict[str, MetricSample] = {}
    for row in samples:
        current = latest_by_node.get(row.node_id)
        if current is None or row.ts > current.ts:
            latest_by_node[row.node_id] = row
    cpu_values = [row.cpu_pct for row in latest_by_node.values() if row.cpu_pct is not None]
    mem_values = [row.mem_used_pct for row in latest_by_node.values() if row.mem_used_pct is not None]
    storage_values = [row.cache_used_pct for row in latest_by_node.values() if row.cache_used_pct is not None]
    concurrent = sum(row.peak_concurrent or row.connections_active or 0 for row in latest_by_node.values() if row.node_id in edge_ids)
    buckets: dict[str, int] = {}
    for row in samples:
        if not row.request_time_bucket:
            continue
        for key, value in row.request_time_bucket.items():
            if isinstance(value, int):
                buckets[key] = buckets.get(key, 0) + value

    node_by_id = {node.id: node for node in nodes}
    peak_mbps = 0.0
    by_region: dict[str, dict] = {}
    for row in samples:
        node = node_by_id.get(row.node_id)
        if node is None or node.tier != "edge" or not row.interval_s:
            continue
        rate = ((row.d_tx_bytes or 0) * 8) / row.interval_s / 1_000_000
        peak_mbps = max(peak_mbps, rate)
        slot = by_region.setdefault(node.region or "unset", {"tx": 0, "interval": 0.0, "nodes": set()})
        slot["tx"] += row.d_tx_bytes or 0
        slot["interval"] += row.interval_s or 0
        slot["nodes"].add(row.node_id)
    region_traffic = []
    for region, slot in sorted(by_region.items()):
        elapsed_region = slot["interval"] / max(len(slot["nodes"]), 1)
        region_rate = (slot["tx"] * 8 / elapsed_region / 1_000_000) if elapsed_region else 0
        region_traffic.append({"region": region, "mbps": round(region_rate, 2)})
    if peak_mbps >= 1000:
        peak_value, peak_unit = round(peak_mbps / 1000, 2), "Gbps"
    else:
        peak_value, peak_unit = round(peak_mbps, 2), "Mbps"
    enabled_origins = [node for node in nodes if node.tier == "origin" and node.enabled]
    origins_up = [node for node in enabled_origins if node.status == "online"]
    saved_pct = round(saved / body * 100, 1) if body and saved is not None else None

    open_alerts = await db.execute(select(func.count()).select_from(Alert).where(Alert.closed_at.is_(None)))
    unmapped = await db.execute(
        select(func.count()).select_from(StreamSighting).where(StreamSighting.unmapped_alerted.is_(True))
    )
    return {
        "cache_hit_ratio_pct": None if chr is None else round(chr, 1),
        "bandwidth_saved_bytes": saved,
        "bandwidth_saved_pct": saved_pct,
        "total_cdn_mbps": round(mbps, 2),
        "origin_traffic_mbps": round((origin_fill * 8 / elapsed / 1_000_000) if elapsed else 0, 2),
        "peak_value": peak_value,
        "peak_unit": peak_unit,
        "concurrent_streams": concurrent,
        "avg_response_ms": round(req_ms / req_n, 1) if req_n else None,
        "p95_response_ms": percentile_from_buckets(buckets),
        "edge_availability_pct": round(100 * len(available) / len(enabled_edges), 1) if enabled_edges else None,
        "origin_availability_pct": round(100 * len(origins_up) / len(enabled_origins), 1) if enabled_origins else None,
        "region_traffic": region_traffic,
        "cpu_pct": round(sum(cpu_values) / len(cpu_values), 1) if cpu_values else None,
        "mem_used_pct": round(sum(mem_values) / len(mem_values), 1) if mem_values else None,
        "storage_used_pct": round(sum(storage_values) / len(storage_values), 1) if storage_values else None,
        "open_alerts": open_alerts.scalar_one(),
        "unmapped_streams": unmapped.scalar_one(),
        "nodes_online": sum(1 for node in nodes if node.status == "online"),
        "nodes_total": len(nodes),
    }


async def top_channels(db: AsyncSession, limit: int = 10) -> list[dict]:
    since = utcnow() - timedelta(hours=6)
    rows = await db.execute(
        select(
            ViewershipStat.stream_uuid,
            func.max(ViewershipStat.peak_concurrent),
            func.max(ViewershipStat.distinct_subscribers_est),
        )
        .where(ViewershipStat.ts >= since)
        .group_by(ViewershipStat.stream_uuid)
        .order_by(func.max(ViewershipStat.peak_concurrent).desc())
        .limit(limit)
    )
    ranked = []
    for stream_uuid, peak, distinct in rows:
        channel = await db.execute(select(Channel).where(Channel.stream_uuid == stream_uuid))
        mapped = channel.scalar_one_or_none()
        ranked.append(
            {
                "stream_uuid": stream_uuid,
                "name": mapped.name if mapped else "unmapped",
                "peak_concurrent": peak or 0,
                "distinct_subscribers_est": distinct or 0,
                "mapped": mapped is not None,
            }
        )
    return ranked


async def series(db: AsyncSession, minutes: int = 60) -> dict:
    since = utcnow() - timedelta(minutes=minutes)
    rows = list((await db.execute(select(MetricSample).where(MetricSample.ts >= since))).scalars())
    nodes = {node.id: node for node in (await db.execute(select(Node))).scalars()}
    buckets: dict[datetime, dict] = {}
    for row in rows:
        key = row.ts.replace(second=0, microsecond=0)
        slot = buckets.setdefault(key, {"tx": 0, "interval": 0, "hit": 0, "miss": 0, "origin": 0, "concurrent": 0, "edges": set()})
        node = nodes.get(row.node_id)
        slot["tx"] += row.d_tx_bytes or 0
        slot["interval"] += row.interval_s or 0
        if node and node.tier == "edge":
            slot["hit"] += row.d_hit_bytes or 0
            slot["miss"] += row.d_miss_bytes or 0
            slot["concurrent"] += row.peak_concurrent or 0
            slot["edges"].add(row.node_id)
        if node and node.tier == "mid":
            slot["origin"] += row.d_upstream_bytes or 0
    labels = []
    traffic = []
    origin = []
    chr = []
    concurrent = []
    for key in sorted(buckets):
        slot = buckets[key]
        elapsed = slot["interval"] / max(len(slot["edges"]) or 1, 1)
        labels.append(key.strftime("%H:%M"))
        traffic.append(round((slot["tx"] * 8 / elapsed / 1_000_000) if elapsed else 0, 2))
        origin.append(round((slot["origin"] * 8 / elapsed / 1_000_000) if elapsed else 0, 2))
        denom = slot["hit"] + slot["miss"]
        chr.append(round(100 * slot["hit"] / denom, 1) if denom else None)
        concurrent.append(slot["concurrent"])
    return {"labels": labels, "traffic_mbps": traffic, "origin_mbps": origin, "chr_pct": chr, "concurrent": concurrent}


def new_secret() -> str:
    return uuid4().hex


def _origin_health(node: Node) -> str:
    if not node.enabled or node.status == "offline":
        return "unavailable"
    return "available"


async def origin_groups(db: AsyncSession) -> list[dict]:
    """Primary origin, and the secondary to use when the primary has failed."""
    result = await db.execute(select(Node).where(Node.tier == "origin").order_by(Node.name))
    nodes = {node.id: node for node in result.scalars()}
    groups = []
    seen: set[str] = set()
    for node in nodes.values():
        if node.origin_role != "primary" or node.id in seen:
            continue
        secondary = nodes.get(node.paired_node_id or "")
        seen.add(node.id)
        if secondary:
            seen.add(secondary.id)
        primary_ok = _origin_health(node) == "available"
        secondary_ok = secondary is not None and _origin_health(secondary) == "available"
        if primary_ok:
            active, reason = node, "primary"
        elif secondary_ok:
            active, reason = secondary, "redirected"
        else:
            active, reason = None, "unavailable"
        groups.append(
            {
                "primary": {"node_id": node.id, "name": node.name, "base_url": node.base_url, "health": _origin_health(node)},
                "secondary": None
                if secondary is None
                else {
                    "node_id": secondary.id,
                    "name": secondary.name,
                    "base_url": secondary.base_url,
                    "health": _origin_health(secondary),
                },
                "active": None if active is None else {"node_id": active.id, "name": active.name, "base_url": active.base_url},
                "redirected": reason == "redirected",
                "status": reason,
            }
        )
    return groups


async def pair_origins(db: AsyncSession, primary_id: str, secondary_id: str, actor: str) -> tuple[int, str]:
    primary = await db.get(Node, primary_id)
    secondary = await db.get(Node, secondary_id)
    if primary is None or secondary is None or primary.tier != "origin" or secondary.tier != "origin":
        return 422, "Choose two origin servers"
    if primary.id == secondary.id:
        return 422, "Primary and secondary must be different origins"
    primary.origin_role = "primary"
    secondary.origin_role = "secondary"
    primary.paired_node_id = secondary.id
    secondary.paired_node_id = primary.id
    await audit(db, actor, "origin.pair", {"primary": primary.name, "secondary": secondary.name})
    return 200, ""


async def capacity_rows(db: AsyncSession, tenant_id: str | None = None) -> list[dict]:
    """Current load against NIC capacity, with a peak-based projection."""
    since = utcnow() - timedelta(minutes=15)
    query = select(Node).where(Node.enabled.is_(True))
    if tenant_id:
        query = query.where(Node.tenant_id == tenant_id)
    nodes = list((await db.execute(query.order_by(Node.region, Node.name))).scalars())
    samples = list((await db.execute(select(MetricSample).where(MetricSample.ts >= since))).scalars())
    by_node: dict[str, list[MetricSample]] = {}
    for row in samples:
        by_node.setdefault(row.node_id, []).append(row)
    report = []
    for node in nodes:
        rows = by_node.get(node.id, [])
        rates = [
            ((row.d_tx_bytes or 0) * 8) / row.interval_s / 1_000_000
            for row in rows
            if row.interval_s
        ]
        current = rates[-1] if rates else 0.0
        peak = max(rates) if rates else 0.0
        projected = peak * 1.15
        nic = node.nic_capacity_mbps or 0
        util = (current / nic * 100) if nic else None
        needs = bool(nic and (current >= 0.7 * nic or projected >= 0.8 * nic))
        report.append(
            {
                "node": node,
                "current_mbps": round(current, 1),
                "peak_mbps": round(peak, 1),
                "projected_mbps": round(projected, 1),
                "capacity_mbps": nic,
                "utilization_pct": None if util is None else round(util, 1),
                "needs_capacity": needs,
            }
        )
    return report


def match_steering(rules: list[SteeringRule], *, ip: str = "", region: str = "", asn: str = "", subscriber: str = "") -> SteeringRule | None:
    ranked = sorted(rules, key=lambda rule: (rule.priority, rule.created_at))
    for rule in ranked:
        if rule.match_type == "ip_cidr" and ip and _ip_in_cidr(ip, rule.match_value):
            return rule
        if rule.match_type == "region" and region and region.lower() == rule.match_value.lower():
            return rule
        if rule.match_type == "asn" and asn and str(asn) == str(rule.match_value):
            return rule
        if rule.match_type == "subscriber_prefix" and subscriber and subscriber.startswith(rule.match_value):
            return rule
    return None


def _ip_in_cidr(ip: str, cidr: str) -> bool:
    import ipaddress

    try:
        if "/" not in cidr:
            return ipaddress.ip_address(ip) == ipaddress.ip_address(cidr)
        return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False


async def align_estate(db: AsyncSession) -> None:
    """Pair the preview origins once, and fill protocol on channels created before this release."""
    origins = list((await db.execute(select(Node).where(Node.tier == "origin"))).scalars())
    by_name = {node.name: node for node in origins}
    primary, secondary = by_name.get("origin-a"), by_name.get("origin-b")
    if primary and secondary and not primary.origin_role and not secondary.origin_role:
        primary.origin_role = "primary"
        secondary.origin_role = "secondary"
        primary.paired_node_id = secondary.id
        secondary.paired_node_id = primary.id
    channels = list((await db.execute(select(Channel))).scalars())
    for channel in channels:
        if not channel.protocol:
            channel.protocol = "hls"
        if not channel.kind:
            channel.kind = "live"
    existing_rules = await db.execute(select(func.count()).select_from(SteeringRule))
    if existing_rules.scalar_one() == 0:
        await _seed_steering(db, by_name)


async def _seed_steering(db: AsyncSession, origins_by_name: dict[str, Node]) -> None:
    del origins_by_name
    nodes = {node.name: node for node in (await db.execute(select(Node))).scalars()}
    tenants = {tenant.code: tenant for tenant in (await db.execute(select(Tenant))).scalars()}
    hyd = nodes.get("edge-hyd-01")
    vja = nodes.get("edge-vja-01")
    acme = tenants.get("ACME-MSO")
    beta = tenants.get("BETA-ISP")
    if hyd and acme:
        db.add(SteeringRule(tenant_id=acme.id, match_type="region", match_value="South", node_id=hyd.id, priority=10, note="ACME South"))
        db.add(SteeringRule(tenant_id=acme.id, match_type="ip_cidr", match_value="203.0.113.0/24", node_id=hyd.id, priority=1, note="ACME lab range"))
    if vja and beta:
        db.add(SteeringRule(tenant_id=beta.id, match_type="region", match_value="South", node_id=vja.id, priority=10, note="Beta South"))
        db.add(SteeringRule(tenant_id=beta.id, match_type="asn", match_value="45820", node_id=vja.id, priority=5, note="Beta network"))
