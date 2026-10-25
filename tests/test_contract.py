"""Contract rules that must keep failing closed."""

import gzip
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Base, DataQualityEvent, Node, SessionRegistration, Tenant, UsageBucket, User, ViewingSession
from app.ops import apply_profile, overlap_seconds
from app.security import hash_password, pseudonym, sha256_hex, sign_body


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sample(epoch: str, body: int, interval: float = 15) -> dict:
    return {
        "ts": _now(),
        "interval_s": interval,
        "counter_epoch": epoch,
        "system": {"cpu_pct": 10, "mem_used_pct": 20, "ntp_offset_ms": 1},
        "storage": [{"mount": "/var/cache/nginx", "role": "cache", "used_pct": 10}],
        "network": [{"name": "bond0", "role": "service", "speed_mbps": 10000, "tx_bytes_total": body, "rx_bytes_total": 10}],
        "http": {
            "requests_total": body // 1000,
            "body_bytes_sent_total": body,
            "status_5xx_total": 1,
            "status_499_total": 1,
            "request_time_sum_ms": 1000,
            "request_time_count": 10,
            "request_time_bucket": {"10": 1, "25": 4, "50": 8, "100": 10, "+Inf": 10},
            "connections_active": 3,
        },
        "cache": {"hit_bytes_total": int(body * 0.8), "miss_bytes_total": int(body * 0.2), "cold": False},
        "upstream": [{"peer": "origin-a:4444", "state": "up", "bytes_received_total": int(body * 0.2)}],
        "service": {"nginx_running": True},
    }


def _batch(node_id: str, sequence: int, samples: list, **extra) -> dict:
    payload = {
        "schema_version": "2.1",
        "batch_id": str(uuid4()),
        "node_id": node_id,
        "node_tier": "edge",
        "agent_version": "1.0.3",
        "sent_at": _now(),
        "sequence": sequence,
        "backfill": False,
        "samples": samples,
    }
    payload.update(extra)
    return payload


def _post(client: TestClient, secret: str, payload: dict):
    raw = gzip.compress(json.dumps(payload).encode())
    return client.post(
        "/api/v1/telemetry",
        content=raw,
        headers={
            "content-type": "application/json",
            "content-encoding": "gzip",
            "x-node-id": payload["node_id"],
            "x-batch-id": payload["batch_id"],
            "x-timestamp": _now(),
            "x-signature": sign_body(secret, raw),
            "x-agent-version": "1.0.3",
        },
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "t.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("SEED_DEMO", "false")
    monkeypatch.setenv("DEMO_TELEMETRY", "false")
    monkeypatch.setenv("BACKGROUND_TASKS", "false")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    application = create_app()
    sync = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync)
    acme = Tenant(
        id="tenant-acme",
        code="ACME-MSO",
        name="ACME",
        salt="acme-tenant-salt",
        profile_version=2,
        profile_fields={
            "subscriber_code": "required",
            "device_mac": "required",
            "drm_session_id": "required",
            "stb_serial": "optional",
            "package_tier": "optional",
            "client_ip": "observed",
        },
        retention_days=90,
        api_key_sha256=sha256_hex("acme-demo-key"),
    )
    beta = Tenant(
        id="tenant-beta",
        code="BETA-ISP",
        name="Beta",
        salt="beta-tenant-salt",
        profile_version=1,
        profile_fields={
            "subscriber_code": "required",
            "device_mac": "not_collected",
            "drm_session_id": "not_collected",
            "client_ip": "observed",
        },
        retention_days=30,
        api_key_sha256=sha256_hex("beta-demo-key"),
    )
    with Session(sync) as session:
        session.add_all([acme, beta])
        session.add_all(
            [
                User(username="admin", password_hash=hash_password("admin123!"), role="super_admin", subscriber_data_read=True),
                User(username="noc", password_hash=hash_password("noc123!"), role="noc"),
                User(
                    username="beta-support",
                    password_hash=hash_password("beta123!"),
                    role="support",
                    tenant_id="tenant-beta",
                    subscriber_data_read=True,
                ),
            ]
        )
        session.add(
            Node(
                id="33333333-3333-4333-8333-333333333331",
                name="edge-hyd-01",
                tier="edge",
                tenant_id="tenant-acme",
                signing_secret="edge-hyd-01-demo-secret",
                enabled=True,
                base_url="https://edge-hyd-01.cdn.example.net",
                priority=1,
                region="South",
                city="Hyderabad",
            )
        )
        session.add(
            Node(
                id="11111111-1111-4111-8111-111111111111",
                name="origin-a",
                tier="origin",
                signing_secret="origin-a-demo-secret",
                enabled=True,
                base_url="http://10.200.0.53:4444",
            )
        )
        session.commit()
    with TestClient(application) as test_client:
        test_client.sync_engine = sync
        yield test_client


def test_login_and_overview(client: TestClient):
    page = client.get("/login")
    assert page.status_code == 200
    assert "CDN Manager" in page.text
    bad = client.post("/login", data={"username": "admin", "password": "nope"})
    assert bad.status_code == 401
    good = client.post("/login", data={"username": "admin", "password": "admin123!"})
    assert good.status_code == 200
    assert "NOC overview" in good.text
    assert client.get("/nodes").status_code == 200


def test_rejects_bad_signature_and_rates_and_wrong_tier(client: TestClient):
    node = "33333333-3333-4333-8333-333333333331"
    payload = _batch(node, 1, [_sample("epoch-1", 1000)])
    raw = gzip.compress(json.dumps(payload).encode())
    bad = client.post(
        "/api/v1/telemetry",
        content=raw,
        headers={
            "content-encoding": "gzip",
            "x-node-id": node,
            "x-batch-id": payload["batch_id"],
            "x-timestamp": _now(),
            "x-signature": "sha256=" + "ab" * 32,
        },
    )
    assert bad.status_code == 401

    rated = _batch(node, 1, [_sample("epoch-1", 1000)])
    rated["samples"][0]["http"]["mbps"] = 12
    assert _post(client, "edge-hyd-01-demo-secret", rated).status_code == 422

    mismatched = _batch(node, 1, [_sample("epoch-1", 1000)])
    mismatched["samples"][0]["origin"] = {"health_endpoint_ok": True}
    assert _post(client, "edge-hyd-01-demo-secret", mismatched).status_code == 422

    old = _batch(node, 1, [_sample("epoch-1", 1000)], schema_version="3.0")
    assert _post(client, "edge-hyd-01-demo-secret", old).status_code == 400


def test_idempotent_batch_epoch_gap_and_negative_delta(client: TestClient):
    node = "33333333-3333-4333-8333-333333333331"
    secret = "edge-hyd-01-demo-secret"
    first = _batch(node, 1, [_sample("epoch-1", 1_000_000)])
    assert _post(client, secret, first).status_code == 202
    again = _post(client, secret, first)
    assert again.status_code == 200
    assert again.json()["duplicate"] is True

    second = _batch(node, 2, [_sample("epoch-1", 1_500_000)])
    assert _post(client, secret, second).status_code == 202

    jumped = _batch(node, 8, [_sample("epoch-1", 1_800_000)])
    assert _post(client, secret, jumped).status_code == 202

    reset = _batch(node, 1, [_sample("epoch-2", 50)])
    assert _post(client, secret, reset).status_code == 202

    backwards = _batch(node, 2, [_sample("epoch-2", 10)])
    assert _post(client, secret, backwards).status_code == 202

    with Session(client.sync_engine) as session:
        gaps = session.execute(select(UsageBucket).where(UsageBucket.gap.is_(True))).scalars().all()
        assert gaps
        quality = session.execute(select(DataQualityEvent).where(DataQualityEvent.kind == "negative_delta")).scalars().all()
        assert quality
        # A backwards counter must not be invoiced as a fresh spike.
        assert all(row.body_bytes >= 0 for row in session.execute(select(UsageBucket)).scalars())


def test_session_profile_and_tenant_isolation(client: TestClient):
    handle_pseudo = pseudonym("acme-tenant-salt", "HYD12345")
    handle = f"1.{handle_pseudo}.Mb0IGxM7YDk"
    created = client.post(
        "/api/v1/sessions/register",
        headers={"authorization": "Bearer acme-demo-key"},
        json={
            "tenant_code": "ACME-MSO",
            "session_handle": handle,
            "subscriber_code": "HYD12345",
            "opened_at": _now(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
            "attributes": {
                "device_mac": "12:23:34:45:56:67",
                "drm_session_id": "wv-1",
                "stb_serial": "STB-1",
                "package_tier": "premium",
                "client_ip": "203.0.113.9",
                "unexpected": "drop-me",
            },
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert "device_mac" in body["stored_attributes"]
    assert "unexpected" in body["dropped_attributes"]
    assert "client_ip" in body["dropped_attributes"]
    replay = client.post(
        "/api/v1/sessions/register",
        headers={"authorization": "Bearer acme-demo-key"},
        json={
            "tenant_code": "ACME-MSO",
            "session_handle": handle,
            "subscriber_code": "HYD12345",
            "opened_at": _now(),
            "expires_at": _now(),
            "attributes": {"device_mac": "12:23:34:45:56:67", "drm_session_id": "wv-1"},
        },
    )
    assert replay.status_code == 200

    beta = client.post(
        "/api/v1/sessions/register",
        headers={"authorization": "Bearer beta-demo-key"},
        json={
            "tenant_code": "BETA-ISP",
            "session_handle": f"1.{pseudonym('beta-tenant-salt', 'VJA1')}.abcdefghijk",
            "subscriber_code": "VJA1",
            "opened_at": _now(),
            "expires_at": _now(),
            "attributes": {"device_mac": "aa:bb:cc:dd:ee:ff", "drm_session_id": "nope"},
        },
    )
    assert beta.status_code == 201
    assert beta.json()["stored_attributes"] == []
    assert "device_mac" in beta.json()["dropped_attributes"]

    leaked = _batch(
        "33333333-3333-4333-8333-333333333331",
        3,
        [_sample("epoch-1", 2_000_000)],
        sessions=[
            {
                "session_id": str(uuid4()),
                "session_handle": handle,
                "stream_uuid": "4f38db7b-b86e-4a65-955a-a8b1a0b05869",
                "started_at": _now(),
                "ended_at": _now(),
                "subscriber_code": "HYD12345",
            }
        ],
    )
    # Sequence may already have moved; use a fresh epoch so the personal-data rule is what fails.
    leaked["sequence"] = 9
    leaked["samples"][0]["counter_epoch"] = "epoch-9"
    assert _post(client, "edge-hyd-01-demo-secret", leaked).status_code == 422

    clean_id = str(uuid4())
    clean = _batch(
        "33333333-3333-4333-8333-333333333331",
        10,
        [_sample("epoch-9", 2_100_000)],
        sessions=[
            {
                "session_id": clean_id,
                "session_handle": handle,
                "subscriber_pseudonym": handle_pseudo,
                "stream_uuid": "4f38db7b-b86e-4a65-955a-a8b1a0b05869",
                "started_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "ended_at": _now(),
                "duration_s": 300,
                "bytes_sent": 1000,
                "bytes_attribution": "exact",
                "client_ip": "203.0.113.45",
            }
        ],
    )
    accepted = _post(client, "edge-hyd-01-demo-secret", clean)
    assert accepted.status_code == 202
    assert accepted.json()["sessions_accepted"] == 1

    with Session(client.sync_engine) as session:
        row = session.execute(select(ViewingSession)).scalar_one()
        assert row.tenant_id == "tenant-acme"
        session_row_id = row.id

    client.post("/logout")
    client.post("/login", data={"username": "noc", "password": "noc123!"})
    denied = client.post(f"/sessions/{session_row_id}/resolve", data={"csrf": _csrf(client)})
    assert denied.status_code == 403

    client.post("/logout")
    client.post("/login", data={"username": "beta-support", "password": "beta123!"})
    hidden = client.post(f"/sessions/{session_row_id}/resolve", data={"csrf": _csrf(client)})
    assert hidden.status_code == 404

    client.post("/logout")
    client.post("/login", data={"username": "admin", "password": "admin123!"})
    shown = client.post(f"/sessions/{session_row_id}/resolve", data={"csrf": _csrf(client)})
    assert shown.status_code == 200
    assert "HYD12345" in shown.text


def test_edge_map_and_channel_reuse(client: TestClient):
    mapped = client.get("/api/v1/edge-map", params={"tenant_code": "ACME-MSO"})
    assert mapped.status_code == 200
    assert mapped.json()["ttl_seconds"] == 60
    assert mapped.json()["edges"][0]["base_url"].startswith("https://edge-hyd-01")

    imported = client.post(
        "/api/v1/channels/import",
        headers={"authorization": "Bearer acme-demo-key"},
        json={"channels": [{"stream_uuid": "230be6d8-cbc4-498f-8211-88dd06d9da02", "name": "Republic TV", "code": "REPUBLIC"}]},
    )
    assert imported.status_code == 200
    with Session(client.sync_engine) as session:
        from app.models import Channel

        channel = session.execute(select(Channel)).scalar_one()
        channel.retired_at = datetime.now(timezone.utc)
        session.commit()
    reused = client.post(
        "/api/v1/channels/import",
        headers={"authorization": "Bearer acme-demo-key"},
        json={"channels": [{"stream_uuid": "230be6d8-cbc4-498f-8211-88dd06d9da02", "name": "Someone Else"}]},
    )
    assert reused.json()["rejected"]


def test_overlap_and_profile_unit():
    start = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 10, 21, 0, tzinfo=timezone.utc)
    assert overlap_seconds(start, end, start + timedelta(minutes=30), end + timedelta(minutes=30)) == 1800
    tenant = Tenant(
        code="ACME-MSO",
        name="ACME",
        salt="s",
        profile_fields={"device_mac": "not_collected", "package_tier": "optional"},
        api_key_sha256="x",
    )
    _stored, names, dropped, missing = apply_profile(tenant, {"device_mac": "aa", "package_tier": "premium"})
    assert names == ["package_tier"]
    assert "device_mac" in dropped
    assert missing == []
    assert len(pseudonym("salt-a", "HYD1")) == 12
    assert pseudonym("salt-a", "HYD1") != pseudonym("salt-b", "HYD1")


def test_origin_failover_steering_and_signed_url(client: TestClient):
    from app.models import Node

    with Session(client.sync_engine) as session:
        primary = session.get(Node, "11111111-1111-4111-8111-111111111111")
        secondary = Node(
            id="11111111-1111-4111-8111-111111111199",
            name="origin-backup",
            tier="origin",
            signing_secret="origin-backup",
            enabled=True,
            status="online",
            base_url="http://10.200.0.99:4444",
            origin_role="secondary",
            paired_node_id=primary.id,
        )
        primary.origin_role = "primary"
        primary.paired_node_id = secondary.id
        primary.status = "offline"
        tenant = session.get(Tenant, "tenant-acme")
        tenant.ip_allowlist = "203.0.113.0/24"
        tenant.signed_urls = True
        session.add(secondary)
        session.commit()

    mapped = client.get("/api/v1/origin-map")
    assert mapped.status_code == 200
    group = mapped.json()["groups"][0]
    assert group["redirected"] is True
    assert group["active"]["name"] == "origin-backup"

    steered = client.get("/api/v1/steering", params={"tenant_code": "ACME-MSO", "ip": "203.0.113.9"})
    assert steered.status_code == 200
    assert steered.json()["edge"]["name"] == "edge-hyd-01"

    denied = client.post(
        "/api/v1/tokens",
        headers={"authorization": "Bearer acme-demo-key"},
        json={"path": "/live/playlist.m3u8", "client_ip": "198.51.100.4"},
    )
    assert denied.status_code == 403
    allowed = client.post(
        "/api/v1/tokens",
        headers={"authorization": "Bearer acme-demo-key"},
        json={"path": "/live/playlist.m3u8", "client_ip": "203.0.113.9"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["signed"] is True
    assert len(allowed.json()["token"]) == 64


def _csrf(client: TestClient) -> str:
    page = client.get("/")
    marker = 'name="csrf" content="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]
