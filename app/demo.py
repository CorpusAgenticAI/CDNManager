"""In-process node agent used to keep the preview console live.

It speaks the same signed, gzipped telemetry contract as a field agent. Counters
are cumulative. The first sample of a process only establishes the baseline.
"""

from __future__ import annotations

import gzip
import json
import random
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.ingest import accept_telemetry
from app.models import Node
from app.security import pseudonym, sign_body

CHANNELS = [
    ("4f38db7b-b86e-4a65-955a-a8b1a0b05869", "9XM", "9XM", "Music"),
    ("230be6d8-cbc4-498f-8211-88dd06d9da02", "Republic TV", "REPUBLIC", "News"),
    ("a1b2c3d4-e5f6-4789-8abc-def012345678", "Star Sports 1", "STARSP1", "Sport"),
    ("b7c8d9e0-f1a2-4456-9abc-1234567890ab", "Colors", "COLORS", "Entertainment"),
    ("c0ffee00-1111-4222-8333-444455556666", "Sun TV", "SUNTV", "Entertainment"),
]
UNMAPPED_STREAM = "8a3f0c11-1111-4111-8111-111111111111"

NODES = [
    {
        "id": "11111111-1111-4111-8111-111111111111",
        "name": "origin-a",
        "tier": "origin",
        "size_class": "L",
        "region": "Headend",
        "city": "Hyderabad",
        "asn": 9498,
        "base_url": "http://10.200.0.53:4444",
        "priority": 1,
        "secret": "origin-a-demo-secret",
        "simulate": True,
        "nic": 10000,
    },
    {
        "id": "11111111-1111-4111-8111-111111111112",
        "name": "origin-b",
        "tier": "origin",
        "size_class": "L",
        "region": "Headend",
        "city": "Hyderabad",
        "asn": 9498,
        "base_url": "http://10.200.0.54:4444",
        "priority": 2,
        "secret": "origin-b-demo-secret",
        "simulate": True,
        "nic": 10000,
    },
    {
        "id": "22222222-2222-4222-8222-222222222221",
        "name": "mid-south",
        "tier": "mid",
        "size_class": "L",
        "region": "South",
        "city": "Hyderabad",
        "asn": 9498,
        "base_url": "https://mid-south.cdn.example.net",
        "priority": 1,
        "secret": "mid-south-demo-secret",
        "simulate": True,
        "nic": 25000,
    },
    {
        "id": "22222222-2222-4222-8222-222222222222",
        "name": "mid-west",
        "tier": "mid",
        "size_class": "L",
        "region": "West",
        "city": "Mumbai",
        "asn": 9498,
        "base_url": "https://mid-west.cdn.example.net",
        "priority": 1,
        "secret": "mid-west-demo-secret",
        "simulate": True,
        "nic": 25000,
    },
    {
        "id": "33333333-3333-4333-8333-333333333331",
        "name": "edge-hyd-01",
        "tier": "edge",
        "size_class": "M",
        "region": "South",
        "city": "Hyderabad",
        "tenant": "ACME-MSO",
        "asn": 24560,
        "base_url": "https://edge-hyd-01.cdn.example.net",
        "priority": 1,
        "secret": "edge-hyd-01-demo-secret",
        "simulate": True,
        "nic": 10000,
    },
    {
        "id": "33333333-3333-4333-8333-333333333332",
        "name": "edge-vja-01",
        "tier": "edge",
        "size_class": "S",
        "region": "South",
        "city": "Vijayawada",
        "tenant": "BETA-ISP",
        "asn": 45820,
        "base_url": "https://edge-vja-01.cdn.example.net",
        "priority": 1,
        "secret": "edge-vja-01-demo-secret",
        "simulate": True,
        "nic": 1000,
    },
    {
        "id": "33333333-3333-4333-8333-333333333333",
        "name": "edge-blr-01",
        "tier": "edge",
        "size_class": "L",
        "region": "South",
        "city": "Bengaluru",
        "tenant": "ACME-MSO",
        "asn": 24309,
        "base_url": "https://edge-blr-01.cdn.example.net",
        "priority": 2,
        "secret": "edge-blr-01-demo-secret",
        "simulate": False,
        "nic": 10000,
    },
]

STATE: dict[str, dict] = {}
HISTOGRAM = ["10", "25", "50", "100", "250", "500", "1000", "2500", "5000", "10000", "+Inf"]


def _defaults(node: Node) -> dict:
    base = 5_000_000_000 + (sum(ord(c) for c in node.name) * 1_000_000)
    buckets = {key: base // 10 for key in HISTOGRAM}
    return {
        "epoch": "a91f3c77b2e40d15",
        "sequence": node.last_sequence or 0,
        "body": base,
        "requests": base // 50_000,
        "hit": int(base * 0.86),
        "miss": int(base * 0.14),
        "tx": int(base * 1.02),
        "rx": int(base * 0.2),
        "upstream": int(base * 0.14),
        "s2xx": base // 50_000,
        "s5xx": 20,
        "s499": 40,
        "s404": 5,
        "request_ms": base // 1_000,
        "request_count": base // 50_000,
        "buckets": buckets,
        "connections": 120 if node.tier == "edge" else 40,
    }


def ensure_state(node: Node) -> dict:
    if node.id in STATE:
        return STATE[node.id]
    counters = node.counters or {}
    if counters:
        state = _defaults(node)
        state["sequence"] = node.last_sequence or 0
        state["epoch"] = node.counter_epoch or state["epoch"]
        state["body"] = int(counters.get("http.body_bytes_sent_total", state["body"]))
        state["requests"] = int(counters.get("http.requests_total", state["requests"]))
        state["hit"] = int(counters.get("cache.hit_bytes_total", state["hit"]))
        state["miss"] = int(counters.get("cache.miss_bytes_total", state["miss"]))
        state["s5xx"] = int(counters.get("http.status_5xx_total", state["s5xx"]))
        state["s499"] = int(counters.get("http.status_499_total", state["s499"]))
        state["request_ms"] = int(counters.get("http.request_time_sum_ms", state["request_ms"]))
        state["request_count"] = int(counters.get("http.request_time_count", state["request_count"]))
        for key, value in counters.items():
            if key.startswith("network[service].tx_bytes_total"):
                state["tx"] = int(value)
            if key.startswith("upstream[") and key.endswith("bytes_received_total"):
                state["upstream"] = int(value)
        STATE[node.id] = state
        return state
    STATE[node.id] = _defaults(node)
    return STATE[node.id]


def advance(node: Node, state: dict, interval: float) -> None:
    viewers = {"S": 180, "M": 1400, "L": 6000}.get(node.size_class, 400)
    if node.tier != "edge":
        viewers = max(20, viewers // 5)
    bitrate = random.randint(2_400_000, 4_200_000)
    body = int(viewers * bitrate / 8 * interval)
    requests = int(viewers * interval / 4)
    hit_share = 0.9 if node.name != "edge-blr-01" else 0.1
    state["body"] += body
    state["requests"] += requests
    state["hit"] += int(body * hit_share)
    state["miss"] += body - int(body * hit_share)
    state["tx"] += int(body * 1.01)
    state["rx"] += int(body * 0.02)
    state["upstream"] += body - int(body * hit_share)
    state["s2xx"] += requests
    state["s5xx"] += 1 if random.random() < 0.05 else 0
    state["s499"] += 1 if random.random() < 0.08 else 0
    state["request_count"] += requests
    state["request_ms"] += requests * random.randint(12, 40)
    state["connections"] = viewers
    count = state["request_count"]
    fractions = {
        "10": 0.12,
        "25": 0.40,
        "50": 0.72,
        "100": 0.90,
        "250": 0.97,
        "500": 0.99,
        "1000": 1,
        "2500": 1,
        "5000": 1,
        "10000": 1,
        "+Inf": 1,
    }
    state["buckets"] = {key: int(count * fraction) for key, fraction in fractions.items()}
    state["sequence"] += 1


def _sample(node: Node, state: dict, ts: datetime, interval: float, include_slow: bool) -> dict:
    cpu = round(random.uniform(18, 55), 1)
    disk = 93.0 if node.name == "edge-blr-01" else round(random.uniform(28, 61), 1)
    sample: dict = {
        "ts": ts.isoformat().replace("+00:00", "Z"),
        "interval_s": interval,
        "counter_epoch": state["epoch"],
        "system": {
            "cpu_pct": cpu,
            "cpu_iowait_pct": round(random.uniform(0.2, 4), 2),
            "mem_used_pct": round(random.uniform(35, 68), 1),
            "mem_available_bytes": 8_000_000_000,
            "ntp_synced": True,
            "ntp_offset_ms": round(random.uniform(-3, 3), 2),
            "load_norm_1m": round(cpu / 100, 2),
            "uptime_s": 86400,
        },
        "storage": [
            {
                "mount": "/var/cache/nginx" if node.tier != "origin" else "/var/origin",
                "role": "cache" if node.tier != "origin" else "other",
                "used_pct": disk if node.tier != "origin" else 41,
                "inodes_used_pct": 12,
                "total_bytes": 4_000_000_000_000,
                "used_bytes": 1_600_000_000_000,
            }
        ],
        "network": [
            {
                "name": "bond0",
                "role": "service",
                "speed_mbps": node.nic_capacity_mbps,
                "oper_state": "up",
                "tx_bytes_total": state["tx"],
                "rx_bytes_total": state["rx"],
            }
        ],
        "http": {
            "requests_total": state["requests"],
            "body_bytes_sent_total": state["body"],
            "bytes_sent_total": int(state["body"] * 1.01),
            "bytes_received_total": state["rx"],
            "status_2xx_total": state["s2xx"],
            "status_4xx_total": state["s404"],
            "status_5xx_total": state["s5xx"],
            "status_499_total": state["s499"],
            "status_404_total": state["s404"],
            "connections_active": state["connections"],
            "request_time_sum_ms": state["request_ms"],
            "request_time_count": state["request_count"],
            "request_time_bucket": dict(state["buckets"]),
        },
        "service": {"nginx_running": True},
        "agent": {"heartbeat": True, "version": "1.0.3"},
    }
    if node.tier != "origin":
        sample["cache"] = {
            "hit_bytes_total": state["hit"],
            "miss_bytes_total": state["miss"],
            "hit_requests_total": int(state["requests"] * 0.9),
            "miss_requests_total": int(state["requests"] * 0.1),
            "evictions_total": 10,
            "keys_count": 12000,
            "cold": False,
        }
        sample["upstream"] = [
            {
                "peer": "origin-a:4444",
                "state": "up",
                "bytes_received_total": state["upstream"],
            }
        ]
    if node.tier == "origin":
        sample["origin"] = {
            "ingest_stalled_channels": 0,
            "newest_segment_age_s": 2.4,
            "replication_state": "ok",
            "replication_lag_s": 1,
            "health_endpoint_ok": True,
        }
        sample["storage"][0]["role"] = "other"
    if include_slow and node.tier == "edge":
        items = []
        for stream_uuid, name, _code, _category in CHANNELS:
            items.append(
                {
                    "key": stream_uuid,
                    "key_type": "stream_uuid",
                    "content_class": "live-segment",
                    "requests": random.randint(200, 4000),
                    "bytes_sent": random.randint(50_000_000, 800_000_000),
                    "hit_bytes": random.randint(40_000_000, 700_000_000),
                    "miss_bytes": random.randint(1_000_000, 40_000_000),
                }
            )
        items.append(
            {
                "key": UNMAPPED_STREAM,
                "key_type": "stream_uuid",
                "content_class": "live-segment",
                "requests": 40,
                "bytes_sent": 8_000_000,
                "hit_bytes": 1_000_000,
                "miss_bytes": 7_000_000,
            }
        )
        sample["content"] = {
            "topn": 200,
            "distinct_keys_seen": len(items),
            "other_requests": 3,
            "other_bytes": 1000,
            "unclassified_requests": 0,
            "unclassified_bytes": 0,
            "items": items,
        }
        sample["viewership"] = {
            "enabled": True,
            "id_source": "path",
            "peak_concurrent": state["connections"],
            "distinct_subscribers_est": int(state["connections"] * 0.92),
            "requests_with_id": state["requests"],
            "requests_without_id": 2,
            "streams": [
                {
                    "stream_uuid": stream_uuid,
                    "distinct_subscribers_est": random.randint(20, max(21, state["connections"] // 3)),
                    "peak_concurrent": random.randint(10, max(11, state["connections"] // 2)),
                    "sessions_started": random.randint(1, 30),
                    "sessions_ended": random.randint(1, 30),
                }
                for stream_uuid, *_rest in CHANNELS
            ],
        }
        sample["clients"] = {
            "distinct_ips_est": state["connections"],
            "by_country": [{"key": "IN", "bytes_sent": state["body"]}],
            "by_asn": [{"asn": node.asn or 0, "bytes_sent": state["body"]}],
        }
        days = 12 if node.name == "edge-vja-01" else 240
        sample["tls"] = {
            "certificates": [
                {"server_name": node.base_url, "days_to_expiry": days, "fingerprint_sha256": "abc123"}
            ]
        }
        sample["config"] = {"active_hash": node.desired_config_hash or "demo-config"}
    if include_slow and node.tier == "mid":
        sample["content"] = {
            "topn": 200,
            "distinct_keys_seen": 2,
            "items": [
                {
                    "key": CHANNELS[0][0],
                    "key_type": "stream_uuid",
                    "content_class": "live-segment",
                    "requests": 100,
                    "bytes_sent": 10_000_000,
                    "hit_bytes": 9_000_000,
                    "miss_bytes": 1_000_000,
                }
            ],
        }
    return sample


def build_batch(node: Node, state: dict, ts: datetime, interval: float = 15.0, include_slow: bool = False, sessions: list | None = None) -> tuple[bytes, dict[str, str]]:
    payload = {
        "schema_version": "2.1",
        "batch_id": str(uuid4()),
        "node_id": node.id,
        "node_tier": node.tier,
        "agent_version": "1.0.3",
        "sent_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sequence": state["sequence"],
        "backfill": False,
        "samples": [_sample(node, state, ts, interval, include_slow)],
    }
    if sessions:
        payload["sessions"] = sessions
    raw = gzip.compress(json.dumps(payload).encode())
    headers = {
        "content-type": "application/json",
        "content-encoding": "gzip",
        "x-node-id": node.id,
        "x-batch-id": payload["batch_id"],
        "x-timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "x-signature": sign_body(node.signing_secret, raw),
        "x-agent-version": "1.0.3",
    }
    return raw, headers


def demo_session(node: Node, salt: str, subscriber: str) -> dict:
    handle_pseudo = pseudonym(salt, subscriber)
    nonce = uuid4().hex[:11]
    now = datetime.now(timezone.utc)
    started = now - timedelta(minutes=12)
    stream = CHANNELS[1][0]
    return {
        "session_id": str(uuid4()),
        "session_handle": f"1.{handle_pseudo}.{nonce}",
        "subscriber_pseudonym": handle_pseudo,
        "stream_uuid": stream,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "ended_at": now.isoformat().replace("+00:00", "Z"),
        "duration_s": 720,
        "requests": 180,
        "playlist_requests": 30,
        "segment_requests": 150,
        "bytes_sent": 240_000_000,
        "bytes_attribution": "exact",
        "error_count": 0,
        "abort_count": 1,
        "client_ip": "203.0.113.45",
        "ua_class": "stb",
        "closed_reason": "idle_timeout",
    }


async def push_sample(
    db: AsyncSession,
    settings: Settings,
    node: Node,
    ts: datetime,
    interval: float = 15.0,
    include_slow: bool = False,
    sessions: list | None = None,
) -> tuple[int, dict]:
    state = ensure_state(node)
    advance(node, state, interval)
    raw, headers = build_batch(node, state, ts, interval, include_slow, sessions)
    return await accept_telemetry(db, settings, raw=raw, headers=headers, client_ip="203.0.113.10")
