"""Telemetry ingest. Implements contract section 14 processing rules."""

from __future__ import annotations

import gzip
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    AgentEvent,
    Alert,
    ContentStat,
    DataQualityEvent,
    EpochBoundary,
    MetricSample,
    Node,
    NodeFact,
    SequenceGap,
    StreamSighting,
    TelemetryBatch,
    UsageBucket,
    ViewershipStat,
    ViewingSession,
    as_utc,
    utcnow,
)
from app.security import signature_ok, truncate_ip

SCHEMA_MAJOR = 2
FORBIDDEN_GROUPS = {
    "edge": {"origin"},
    "mid": {"viewership", "origin"},
    "origin": {"cache", "upstream", "viewership", "clients"},
}
GROUP_NAMES = (
    "system",
    "storage",
    "network",
    "http",
    "cache",
    "upstream",
    "tls",
    "content",
    "viewership",
    "clients",
    "service",
    "config",
    "agent",
    "origin",
)
PERSONAL_KEYS = {
    "subscriber_code",
    "device_mac",
    "drm_session_id",
    "stb_serial",
    "package_tier",
}
ALLOWED_MBPS = {"speed_mbps", "nic_capacity_mbps"}
HISTOGRAM_KEYS = ("10", "25", "50", "100", "250", "500", "1000", "2500", "5000", "10000", "+Inf")


class IngestError(Exception):
    def __init__(self, status: int, body: dict):
        self.status = status
        self.body = body


def gunzip_limited(raw: bytes, limit: int) -> bytes:
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as handle:
        out = handle.read(limit + 1)
    if len(out) > limit:
        raise IngestError(413, {"error": "decompressed payload exceeds 8 MB"})
    return out


def parse_time(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def find_forbidden_mbps(obj: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}" if path else key
            if (key == "mbps" or key.endswith("_mbps")) and key not in ALLOWED_MBPS:
                found.append(here)
            found.extend(find_forbidden_mbps(value, here))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            found.extend(find_forbidden_mbps(value, f"{path}[{index}]"))
    return found


def flatten_counters(obj: Any, prefix: str = "") -> dict[str, int]:
    out: dict[str, int] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and not isinstance(value, bool) and key.endswith("_total"):
                out[path] = value
            elif isinstance(value, dict):
                out.update(flatten_counters(value, path))
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    label = str(index)
                    if isinstance(item, dict):
                        label = str(
                            item.get("role")
                            or item.get("name")
                            or item.get("peer")
                            or item.get("mount")
                            or index
                        )
                    out.update(flatten_counters(item, f"{path}[{label}]"))
    return out


def bucket_start(ts: datetime, width_s: int) -> datetime:
    epoch = int(ts.timestamp())
    floored = epoch - (epoch % width_s)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _int(value: Any) -> int | None:
    number = _num(value)
    if number is None:
        return None
    return int(number)


async def open_alert(
    db: AsyncSession,
    *,
    node: Node | None,
    type_: str,
    severity: str,
    message: str,
    detail: dict | None = None,
    tenant_id: str | None = None,
) -> Alert | None:
    node_id = node.id if node else None
    existing = await db.execute(
        select(Alert).where(
            Alert.type == type_,
            Alert.node_id == node_id,
            Alert.closed_at.is_(None),
        )
    )
    current = existing.scalar_one_or_none()
    if current is not None:
        current.message = message
        current.detail = detail or {}
        current.severity = severity
        return None
    alert = Alert(
        node_id=node_id,
        tenant_id=tenant_id if tenant_id is not None else (node.tenant_id if node else None),
        type=type_,
        severity=severity,
        message=message,
        detail=detail or {},
    )
    db.add(alert)
    await db.flush()
    return alert


async def close_alert(db: AsyncSession, node_id: str | None, type_: str) -> None:
    result = await db.execute(
        select(Alert).where(Alert.type == type_, Alert.node_id == node_id, Alert.closed_at.is_(None))
    )
    for alert in result.scalars():
        alert.closed_at = utcnow()


def service_tx(network: Any) -> int | None:
    if not isinstance(network, list):
        return None
    total = 0
    found = False
    for iface in network:
        if not isinstance(iface, dict):
            continue
        if iface.get("role") not in (None, "service"):
            continue
        value = iface.get("tx_bytes_total")
        if isinstance(value, int):
            total += value
            found = True
    return total if found else None


def upstream_bytes(upstream: Any) -> int | None:
    if not isinstance(upstream, list):
        return None
    total = 0
    found = False
    for peer in upstream:
        if isinstance(peer, dict) and isinstance(peer.get("bytes_received_total"), int):
            total += peer["bytes_received_total"]
            found = True
    return total if found else None


def cache_used_pct(storage: Any) -> float | None:
    if not isinstance(storage, list):
        return None
    for fs in storage:
        if isinstance(fs, dict) and fs.get("role") == "cache" and _num(fs.get("used_pct")) is not None:
            return float(fs["used_pct"])
    return None


async def note_stream(
    db: AsyncSession,
    stream_uuid: str,
    ts: datetime,
    requests: int,
    byte_count: int,
) -> None:
    row = await db.get(StreamSighting, stream_uuid)
    if row is None:
        db.add(
            StreamSighting(
                stream_uuid=stream_uuid,
                first_seen_at=ts,
                last_seen_at=ts,
                requests=requests,
                bytes_sent=byte_count,
            )
        )
        await db.flush()
        return
    row.last_seen_at = ts
    row.requests += requests
    row.bytes_sent += byte_count


async def accept_telemetry(
    db: AsyncSession,
    settings: Settings,
    *,
    raw: bytes,
    headers: dict[str, str],
    client_ip: str,
) -> tuple[int, dict]:
    """Verify, validate, and store one telemetry POST. Returns HTTP status and body."""
    lowered = {k.lower(): v for k, v in headers.items()}
    if len(raw) > settings.telemetry_max_raw_bytes:
        return 413, {"error": "compressed body too large"}

    node_id = lowered.get("x-node-id", "")
    node = await db.get(Node, node_id) if node_id else None
    if node is None or not signature_ok(node.signing_secret, raw, lowered.get("x-signature", "")):
        return 401, {"error": "signature rejected"}

    stamp = lowered.get("x-timestamp", "")
    try:
        signed_at = parse_time(stamp)
    except Exception:
        return 401, {"error": "X-Timestamp is not RFC3339"}
    skew = abs((datetime.now(timezone.utc) - signed_at).total_seconds())
    if skew > settings.telemetry_max_skew_s:
        return 401, {"error": "X-Timestamp skew exceeds 300s"}
    if not node.enabled:
        return 403, {"error": "node disabled"}

    encoding = lowered.get("content-encoding", "").lower()
    try:
        if encoding == "gzip":
            inflated = gunzip_limited(raw, settings.telemetry_max_inflated_bytes)
        elif encoding in ("", "identity"):
            inflated = raw
        else:
            return 415, {"error": "unsupported content encoding"}
    except IngestError as exc:
        return exc.status, exc.body
    except Exception:
        return 400, {"error": "gzip decode failed"}

    try:
        payload = json.loads(inflated)
    except json.JSONDecodeError:
        return 400, {"error": "body is not JSON"}
    if not isinstance(payload, dict):
        return 400, {"error": "body must be an object"}

    mbps = find_forbidden_mbps(payload)
    if mbps:
        return 422, {"error": "rates are not accepted", "fields": mbps}

    version = str(payload.get("schema_version", ""))
    major = version.split(".", 1)[0]
    if major != str(SCHEMA_MAJOR):
        return 400, {"error": "unimplemented schema major version", "schema_version": version}

    if payload.get("node_id") != node.id or lowered.get("x-batch-id") != payload.get("batch_id"):
        await open_alert(
            db,
            node=node,
            type_="node_id_mismatch",
            severity="critical",
            message="Telemetry identity did not match the signing node",
        )
        return 401, {"error": "node or batch identity mismatch"}

    batch_id = str(payload.get("batch_id", ""))
    seen = await db.execute(
        select(TelemetryBatch).where(
            TelemetryBatch.node_id == node.id,
            TelemetryBatch.batch_id == batch_id,
        )
    )
    if seen.scalar_one_or_none() is not None:
        return 200, {"duplicate": True}

    tier = payload.get("node_tier")
    if tier != node.tier:
        return 422, {"error": "node_tier does not match the registered node"}

    samples = payload.get("samples")
    sessions = payload.get("sessions") or []
    events = payload.get("events") or []
    facts = payload.get("facts")
    if not isinstance(samples, list) or not 1 <= len(samples) <= 240:
        return 422, {"error": "samples must contain 1 to 240 objects"}
    if not isinstance(sessions, list) or len(sessions) > 1000:
        return 422, {"error": "sessions must contain at most 1000 objects"}
    if not isinstance(events, list) or len(events) > 100:
        return 422, {"error": "events must contain at most 100 objects"}
    if sessions and node.tier != "edge":
        return 422, {"error": "viewing sessions are edge-only"}
    if sessions and node.tenant_id is None:
        return 422, {"error": "edge has no tenant; sessions cannot be scoped"}

    forbidden = FORBIDDEN_GROUPS[node.tier]
    parsed_samples: list[dict] = []
    for item in samples:
        if not isinstance(item, dict):
            return 422, {"error": "sample must be an object"}
        present = {name for name in GROUP_NAMES if name in item and item[name] is not None}
        bad = sorted(present & forbidden)
        if bad:
            return 422, {"error": "tier/group mismatch", "groups": bad, "node_tier": node.tier}
        try:
            interval = float(item["interval_s"])
            ts = parse_time(str(item["ts"]))
            epoch = str(item["counter_epoch"])
        except Exception:
            return 422, {"error": "sample requires ts, interval_s and counter_epoch"}
        if interval <= 0 or interval > 900:
            return 422, {"error": "interval_s must be within (0, 900]"}
        parsed_samples.append({"raw": item, "ts": ts, "interval_s": interval, "epoch": epoch})

    for session in sessions:
        if not isinstance(session, dict):
            return 422, {"error": "session must be an object"}
        leaked = sorted(PERSONAL_KEYS & set(session))
        if leaked:
            return 422, {"error": "personal data is not accepted from a node", "fields": leaked}
        missing = [key for key in ("session_id", "session_handle", "stream_uuid", "started_at", "ended_at") if not session.get(key)]
        if missing:
            return 422, {"error": "session is missing an identity or time field", "fields": missing}

    parsed_samples.sort(key=lambda row: row["ts"])
    try:
        sent_at = parse_time(str(payload.get("sent_at")))
        sequence = int(payload["sequence"])
    except Exception:
        return 422, {"error": "sent_at and sequence are required"}

    gap = False
    if node.counter_epoch and node.counter_epoch == parsed_samples[-1]["epoch"]:
        if sequence > node.last_sequence + 1:
            gap = True
            db.add(
                SequenceGap(
                    node_id=node.id,
                    counter_epoch=node.counter_epoch,
                    expected_sequence=node.last_sequence + 1,
                    got_sequence=sequence,
                )
            )
        elif sequence <= node.last_sequence and not payload.get("backfill"):
            return 409, {"error": "sequence regression inside the current counter epoch"}

    sessions_accepted = 0
    new_alerts: list[Alert] = []
    try:
        for sample in parsed_samples:
            alert = await _store_sample(db, settings, node, sample, gap)
            if alert is not None:
                new_alerts.append(alert)
        for session in sessions:
            inserted = await _store_session(db, node, session)
            sessions_accepted += int(inserted)
        for event in events:
            alert = await _store_event(db, node, event)
            if alert is not None:
                new_alerts.append(alert)
        if isinstance(facts, dict):
            db.add(NodeFact(node_id=node.id, facts=facts, reported_at=sent_at))
            if isinstance(facts.get("public_ipv4"), str):
                node.public_ipv4 = facts["public_ipv4"]

        db.add(
            TelemetryBatch(
                node_id=node.id,
                batch_id=batch_id,
                sequence=sequence,
                schema_version=version,
                sent_at=sent_at,
                backfill=bool(payload.get("backfill")),
                sample_count=len(parsed_samples),
                session_count=sessions_accepted,
            )
        )
        node.last_seen_at = utcnow()
        node.agent_version = str(payload.get("agent_version") or node.agent_version)
        node.observed_ip = client_ip
        node.status = "online"
        node.last_sequence = sequence
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return 200, {"duplicate": True}

    return 202, {
        "accepted": len(parsed_samples),
        "sessions_accepted": sessions_accepted,
        "next_interval_s": 15 if node.tier != "origin" else 15,
        "observed_ip": client_ip,
        "alerts_opened": len(new_alerts),
    }


async def _store_sample(
    db: AsyncSession,
    settings: Settings,
    node: Node,
    sample: dict,
    batch_gap: bool,
) -> Alert | None:
    raw = sample["raw"]
    epoch = sample["epoch"]
    ts: datetime = sample["ts"]
    interval = sample["interval_s"]
    previous = dict(node.counters or {})
    same_epoch = bool(node.counter_epoch) and node.counter_epoch == epoch
    if node.counter_epoch and node.counter_epoch != epoch:
        reported = any(
            isinstance(raw.get("agent"), dict) and raw["agent"].get("counter_reset_detected")
            for _ in (0,)
        )
        db.add(
            EpochBoundary(
                node_id=node.id,
                previous_epoch=node.counter_epoch,
                new_epoch=epoch,
                at=ts,
                agent_reported=reported,
            )
        )
        if not reported:
            db.add(
                AgentEvent(
                    node_id=node.id,
                    event_id=f"reset-{epoch}",
                    ts=ts,
                    type="counter_reset_detected",
                    severity="info",
                    detail={"previous_epoch": node.counter_epoch, "cause": "epoch change without agent notice"},
                )
            )
        previous = {}
        same_epoch = False

    flat = flatten_counters(raw)
    http_for_hist = raw.get("http") if isinstance(raw.get("http"), dict) else {}
    bucket = http_for_hist.get("request_time_bucket")
    if isinstance(bucket, dict):
        for key, value in bucket.items():
            if isinstance(value, int) and not isinstance(value, bool):
                flat[f"http.request_time_bucket.{key}"] = value
    deltas: dict[str, int | None] = {}
    if same_epoch and previous:
        for key, value in flat.items():
            old = previous.get(key)
            if isinstance(old, int):
                delta = value - old
                if delta < 0:
                    deltas[key] = None
                    db.add(
                        DataQualityEvent(
                            node_id=node.id,
                            kind="negative_delta",
                            detail={"field": key, "previous": old, "current": value},
                            at=ts,
                        )
                    )
                else:
                    deltas[key] = delta
    node.counters = flat
    node.counter_epoch = epoch

    system = raw.get("system") if isinstance(raw.get("system"), dict) else {}
    http = raw.get("http") if isinstance(raw.get("http"), dict) else {}
    cache = raw.get("cache") if isinstance(raw.get("cache"), dict) else {}
    service = raw.get("service") if isinstance(raw.get("service"), dict) else {}
    viewership = raw.get("viewership") if isinstance(raw.get("viewership"), dict) else {}
    content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
    tls = raw.get("tls") if isinstance(raw.get("tls"), dict) else {}
    config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
    origin = raw.get("origin") if isinstance(raw.get("origin"), dict) else {}

    def delta_of(*keys: str) -> int | None:
        for key in keys:
            if key in deltas:
                return deltas[key]
        return None

    def sum_delta(prefix: str, suffix: str) -> int | None:
        matched = [value for key, value in deltas.items() if key.startswith(prefix) and key.endswith(suffix)]
        if not matched:
            return None
        if any(value is None for value in matched):
            return None
        return int(sum(value or 0 for value in matched))

    d_body = delta_of("http.body_bytes_sent_total")
    d_requests = delta_of("http.requests_total")
    d_5xx = delta_of("http.status_5xx_total")
    d_499 = delta_of("http.status_499_total")
    d_hit = delta_of("cache.hit_bytes_total")
    d_miss = delta_of("cache.miss_bytes_total")
    d_tx = sum_delta("network[", ".tx_bytes_total")
    d_up = sum_delta("upstream[", ".bytes_received_total")
    d_ms = delta_of("http.request_time_sum_ms")
    d_count = delta_of("http.request_time_count")
    bucket_delta = _histogram_delta(previous, flat, http.get("request_time_bucket"))

    row = MetricSample(
        node_id=node.id,
        ts=ts,
        interval_s=interval,
        counter_epoch=epoch,
        cpu_pct=_num(system.get("cpu_pct")),
        mem_used_pct=_num(system.get("mem_used_pct")),
        cache_used_pct=cache_used_pct(raw.get("storage")),
        connections_active=_int(http.get("connections_active")),
        peak_concurrent=_int(viewership.get("peak_concurrent")),
        ntp_offset_ms=_num(system.get("ntp_offset_ms")),
        nginx_running=service.get("nginx_running") if isinstance(service.get("nginx_running"), bool) else None,
        d_body_bytes=d_body,
        d_tx_bytes=d_tx,
        d_upstream_bytes=d_up,
        d_requests=d_requests,
        d_5xx=d_5xx,
        d_499=d_499,
        d_hit_bytes=d_hit,
        d_miss_bytes=d_miss,
        d_request_ms=d_ms,
        d_request_count=d_count,
        request_time_bucket=bucket_delta,
        cold_cache=cache.get("cold") if isinstance(cache.get("cold"), bool) else None,
        payload=raw,
    )
    db.add(row)
    if isinstance(service.get("nginx_running"), bool):
        node.nginx_running = service["nginx_running"]

    if d_body is not None and node.tenant_id:
        start = bucket_start(ts, settings.usage_bucket_s)
        existing = await db.execute(
            select(UsageBucket).where(UsageBucket.node_id == node.id, UsageBucket.bucket_start == start)
        )
        usage = existing.scalar_one_or_none()
        if usage is None:
            usage = UsageBucket(
                tenant_id=node.tenant_id,
                node_id=node.id,
                bucket_start=start,
                body_bytes=0,
                requests=0,
            )
            db.add(usage)
            await db.flush()
        usage.body_bytes += d_body
        usage.requests += d_requests or 0
        if batch_gap:
            usage.gap = True
    elif same_epoch and previous and any(value is None for key, value in deltas.items() if key.startswith("http.body_")):
        start = bucket_start(ts, settings.usage_bucket_s)
        existing = await db.execute(
            select(UsageBucket).where(UsageBucket.node_id == node.id, UsageBucket.bucket_start == start)
        )
        usage = existing.scalar_one_or_none()
        if usage is not None:
            usage.quality_hold = True

    await _content_and_viewership(db, node, ts, content, viewership)
    return await _evaluate_sample_alerts(db, node, raw, interval, d_requests, d_5xx, d_499, d_hit, d_miss, d_tx, tls, config, origin, cache)


def _histogram_delta(previous: dict, flat: dict, current_bucket: Any) -> dict | None:
    if not isinstance(current_bucket, dict):
        return None
    out: dict[str, int] = {}
    for key in HISTOGRAM_KEYS:
        path = f"http.request_time_bucket.{key}"
        if path not in flat:
            continue
        old = previous.get(path)
        if isinstance(old, int):
            delta = flat[path] - old
            out[key] = delta if delta >= 0 else 0
        else:
            out[key] = 0
    return out or None


async def _content_and_viewership(db: AsyncSession, node: Node, ts: datetime, content: dict, viewership: dict) -> None:
    items = content.get("items") if isinstance(content.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        if not key or key in ("__other__", "__unclassified__"):
            continue
        requests = int(item.get("requests") or 0)
        byte_count = int(item.get("bytes_sent") or 0)
        db.add(
            ContentStat(
                node_id=node.id,
                ts=ts,
                stream_uuid=key,
                key_type=str(item.get("key_type") or "stream_uuid"),
                requests=requests,
                bytes_sent=byte_count,
                hit_bytes=int(item.get("hit_bytes") or 0),
                miss_bytes=int(item.get("miss_bytes") or 0),
            )
        )
        await note_stream(db, key, ts, requests, byte_count)
        from app.models import Channel

        channel = await db.execute(select(Channel).where(Channel.stream_uuid == key))
        mapped = channel.scalar_one_or_none()
        if mapped is None:
            sighting = await db.get(StreamSighting, key)
            if sighting is not None and not sighting.unmapped_alerted:
                sighting.unmapped_alerted = True
                await open_alert(
                    db,
                    node=node,
                    type_="unmapped_stream_seen",
                    severity="minor",
                    message=f"Unmapped stream {key}",
                    detail={"stream_uuid": key},
                )
        else:
            mapped.first_seen_at = mapped.first_seen_at or ts
            mapped.last_seen_at = ts

    streams = viewership.get("streams") if isinstance(viewership.get("streams"), list) else []
    for stream in streams:
        if not isinstance(stream, dict) or not stream.get("stream_uuid"):
            continue
        db.add(
            ViewershipStat(
                node_id=node.id,
                ts=ts,
                stream_uuid=str(stream["stream_uuid"]),
                distinct_subscribers_est=int(stream.get("distinct_subscribers_est") or 0),
                peak_concurrent=int(stream.get("peak_concurrent") or 0),
                sessions_started=int(stream.get("sessions_started") or 0),
                sessions_ended=int(stream.get("sessions_ended") or 0),
            )
        )


async def _evaluate_sample_alerts(
    db: AsyncSession,
    node: Node,
    raw: dict,
    interval: float,
    d_requests: int | None,
    d_5xx: int | None,
    d_499: int | None,
    d_hit: int | None,
    d_miss: int | None,
    d_tx: int | None,
    tls: dict,
    config: dict,
    origin: dict,
    cache: dict,
) -> Alert | None:
    opened = None
    storage = raw.get("storage") if isinstance(raw.get("storage"), list) else []
    disk_hot = False
    for fs in storage:
        if not isinstance(fs, dict):
            continue
        used = _num(fs.get("used_pct"))
        inodes = _num(fs.get("inodes_used_pct"))
        if fs.get("role") == "cache" and used is not None and used >= 90:
            disk_hot = True
            opened = await open_alert(
                db,
                node=node,
                type_="storage_exhaustion",
                severity="major" if used < 95 else "critical",
                message=f"Cache volume {fs.get('mount', '')} is {used:.0f}% full",
                detail={"used_pct": used, "mount": fs.get("mount")},
            ) or opened
        if inodes is not None and inodes >= 90:
            opened = await open_alert(
                db,
                node=node,
                type_="inode_exhaustion",
                severity="major",
                message=f"Inodes at {inodes:.0f}% on {fs.get('mount', '')}",
            ) or opened
    if not disk_hot:
        await close_alert(db, node.id, "storage_exhaustion")

    network = raw.get("network") if isinstance(raw.get("network"), list) else []
    high_bw = False
    abnormal = False
    for iface in network:
        if not isinstance(iface, dict) or iface.get("role") != "service":
            continue
        speed = _num(iface.get("speed_mbps"))
        if d_tx is None or not speed or interval <= 0:
            continue
        mbps = (d_tx * 8) / interval / 1_000_000
        if mbps >= 0.8 * speed:
            high_bw = True
            opened = await open_alert(
                db,
                node=node,
                type_="high_bandwidth",
                severity="major",
                message=f"{iface.get('name')} at {mbps:.0f} Mbps of {speed:.0f}",
                detail={"mbps": round(mbps, 1), "speed_mbps": speed},
            ) or opened
        if mbps >= speed:
            abnormal = True
            opened = await open_alert(
                db,
                node=node,
                type_="abnormal_traffic",
                severity="critical",
                message=f"{iface.get('name')} is above link capacity at {mbps:.0f} Mbps",
                detail={"mbps": round(mbps, 1), "speed_mbps": speed},
            ) or opened
    if not high_bw:
        await close_alert(db, node.id, "high_bandwidth")
    if not abnormal:
        await close_alert(db, node.id, "abnormal_traffic")

    degraded = False
    if d_requests and d_requests > 0:
        if d_5xx is not None and d_5xx / d_requests >= 0.02:
            degraded = True
        if d_499 is not None and d_499 / d_requests >= 0.05:
            degraded = True
    if degraded:
        opened = await open_alert(
            db,
            node=node,
            type_="service_degradation",
            severity="major",
            message="5xx or client-abort rate is above the service threshold",
        ) or opened
    else:
        await close_alert(db, node.id, "service_degradation")

    cold = cache.get("cold") is True
    if d_hit is not None and d_miss is not None and (d_hit + d_miss) > 0 and not cold:
        ratio = d_hit / (d_hit + d_miss)
        if ratio < 0.2 and (d_requests or 0) > 20:
            opened = await open_alert(
                db,
                node=node,
                type_="cache_hit_collapse",
                severity="major",
                message=f"Cache hit ratio {ratio:.0%} while the cache is warm",
            ) or opened
        else:
            await close_alert(db, node.id, "cache_hit_collapse")

    system = raw.get("system") if isinstance(raw.get("system"), dict) else {}
    offset = _num(system.get("ntp_offset_ms"))
    if offset is not None and abs(offset) > 5000:
        opened = await open_alert(
            db,
            node=node,
            type_="clock_skew",
            severity="major",
            message=f"NTP offset {offset:.0f} ms",
        ) or opened
    elif offset is not None:
        await close_alert(db, node.id, "clock_skew")

    certs = tls.get("certificates") if isinstance(tls.get("certificates"), list) else []
    expiring = False
    for cert in certs:
        if isinstance(cert, dict) and _num(cert.get("days_to_expiry")) is not None:
            days = float(cert["days_to_expiry"])
            if days <= 21:
                expiring = True
                opened = await open_alert(
                    db,
                    node=node,
                    type_="cert_expiring",
                    severity="major",
                    message=f"Certificate {cert.get('server_name', '')} expires in {days:.0f} days",
                    detail={"days_to_expiry": days},
                ) or opened
    if not expiring and certs:
        await close_alert(db, node.id, "cert_expiring")

    active_hash = config.get("active_hash")
    if node.desired_config_hash and isinstance(active_hash, str) and active_hash != node.desired_config_hash:
        opened = await open_alert(
            db,
            node=node,
            type_="config_drift",
            severity="minor",
            message="Running configuration hash does not match the desired hash",
            detail={"expected": node.desired_config_hash, "active": active_hash},
        ) or opened
    elif node.desired_config_hash and isinstance(active_hash, str):
        await close_alert(db, node.id, "config_drift")

    stalled = _int(origin.get("ingest_stalled_channels")) or 0
    age = _num(origin.get("newest_segment_age_s"))
    if stalled > 0 or (age is not None and age > 30):
        opened = await open_alert(
            db,
            node=node,
            type_="ingest_stalled",
            severity="critical",
            message="Origin is not seeing fresh packager segments",
            detail={"ingest_stalled_channels": stalled, "newest_segment_age_s": age},
        ) or opened
    elif origin:
        await close_alert(db, node.id, "ingest_stalled")

    if origin.get("replication_state") == "broken":
        opened = await open_alert(
            db,
            node=node,
            type_="replication_broken",
            severity="critical",
            message="Origin replication is broken",
            detail={"lag_s": origin.get("replication_lag_s")},
        ) or opened
    elif origin.get("replication_state") == "ok":
        await close_alert(db, node.id, "replication_broken")
    return opened


async def _store_session(db: AsyncSession, node: Node, session: dict) -> bool:
    ip = str(session.get("client_ip") or "")
    from app.models import Tenant

    if node.tenant_id and ip:
        tenant = await db.get(Tenant, node.tenant_id)
        if tenant and tenant.truncate_client_ip:
            ip = truncate_ip(ip)
    try:
        async with db.begin_nested():
            await _insert_session(db, node, session, ip)
    except IntegrityError:
        return False
    return True


async def _insert_session(db: AsyncSession, node: Node, session: dict, ip: str) -> None:
    row = ViewingSession(
        node_id=node.id,
        tenant_id=node.tenant_id,
        session_id=str(session["session_id"]),
        session_handle=str(session["session_handle"]),
        subscriber_pseudonym=str(session.get("subscriber_pseudonym") or ""),
        stream_uuid=str(session["stream_uuid"]),
        started_at=parse_time(str(session["started_at"])),
        ended_at=parse_time(str(session["ended_at"])),
        duration_s=int(session.get("duration_s") or 0),
        requests=int(session.get("requests") or 0),
        playlist_requests=int(session.get("playlist_requests") or 0),
        segment_requests=int(session.get("segment_requests") or 0),
        bytes_sent=int(session.get("bytes_sent") or 0),
        bytes_attribution=str(session.get("bytes_attribution") or "exact"),
        error_count=int(session.get("error_count") or 0),
        abort_count=int(session.get("abort_count") or 0),
        client_ip=ip,
        ua_class=str(session.get("ua_class") or ""),
        closed_reason=str(session.get("closed_reason") or ""),
        continuation_of=str(session.get("continuation_of") or ""),
    )
    db.add(row)
    await db.flush()
    from app.models import Channel

    mapped = await db.execute(select(Channel).where(Channel.stream_uuid == row.stream_uuid))
    if mapped.scalar_one_or_none() is None:
        await note_stream(db, row.stream_uuid, row.ended_at, row.requests, row.bytes_sent)
        sighting = await db.get(StreamSighting, row.stream_uuid)
        if sighting is not None and not sighting.unmapped_alerted:
            sighting.unmapped_alerted = True
            await open_alert(
                db,
                node=node,
                type_="unmapped_stream_seen",
                severity="minor",
                message=f"Unmapped stream {row.stream_uuid}",
                detail={"stream_uuid": row.stream_uuid},
            )


async def _store_event(db: AsyncSession, node: Node, event: dict) -> Alert | None:
    if not isinstance(event, dict) or not event.get("event_id") or not event.get("type"):
        raise IngestError(422, {"error": "event requires event_id and type"})
    db.add(
        AgentEvent(
            node_id=node.id,
            event_id=str(event["event_id"]),
            ts=parse_time(str(event.get("ts") or utcnow().isoformat())),
            type=str(event["type"]),
            severity=str(event.get("severity") or "info"),
            detail=event.get("detail") if isinstance(event.get("detail"), dict) else {},
        )
    )
    severity = str(event.get("severity") or "info")
    if severity in {"major", "critical"}:
        return await open_alert(
            db,
            node=node,
            type_=str(event["type"]),
            severity=severity,
            message=str(event["type"]).replace("_", " "),
            detail=event.get("detail") if isinstance(event.get("detail"), dict) else {},
        )
    return None


async def mark_offline_nodes(db: AsyncSession, offline_after_s: int) -> list[Alert]:
    cutoff = utcnow() - timedelta(seconds=offline_after_s)
    result = await db.execute(select(Node).where(Node.enabled.is_(True)))
    opened: list[Alert] = []
    for node in result.scalars():
        stale = node.last_seen_at is None or as_utc(node.last_seen_at) < cutoff
        if stale and node.status != "offline":
            node.status = "offline"
            message = f"{node.name} has missed telemetry"
            alert_type = "edge_offline" if node.tier == "edge" else "node_offline"
            if node.tier == "origin":
                alert_type = "origin_failure"
                partner = await db.get(Node, node.paired_node_id) if node.paired_node_id else None
                if partner and partner.enabled and partner.status != "offline":
                    message = f"{node.name} failed; redirect traffic to {partner.name}"
                else:
                    message = f"{node.name} failed and no secondary origin is available"
            alert = await open_alert(
                db,
                node=node,
                type_=alert_type,
                severity="critical" if node.tier == "origin" else "major",
                message=message,
            )
            if alert:
                opened.append(alert)
        elif not stale:
            await close_alert(db, node.id, "edge_offline")
            await close_alert(db, node.id, "node_offline")
            await close_alert(db, node.id, "origin_failure")
            if node.status == "offline":
                node.status = "online"
    return opened
