"""Demo estate: two franchisees, a headend, and a short telemetry history."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.demo import CHANNELS, NODES, UNMAPPED_STREAM, demo_session, push_sample
from app.models import EpgProgramme, Node, SyslogMessage, Tenant, User
from app.ops import import_channels, register_session
from app.security import hash_password, sha256_hex

ACME_KEY = "acme-demo-key"
BETA_KEY = "beta-demo-key"
ACME_SALT = "acme-tenant-salt"
BETA_SALT = "beta-tenant-salt"


async def bootstrap_admin(db: AsyncSession, settings: Settings) -> bool:
    """Create the first super admin on an empty users table. Later restarts never reset the password."""
    if not settings.bootstrap_admin_username or not settings.bootstrap_admin_password:
        return False
    if (await db.execute(select(func.count()).select_from(User))).scalar_one():
        return False
    db.add(
        User(
            username=settings.bootstrap_admin_username,
            password_hash=hash_password(settings.bootstrap_admin_password),
            role="super_admin",
        )
    )
    await db.flush()
    return True


async def seed_demo(db: AsyncSession, settings: Settings) -> None:
    existing = await db.execute(select(User).where(User.username == "admin"))
    if existing.scalar_one_or_none() is not None:
        return

    acme = Tenant(
        code="ACME-MSO",
        name="ACME MSO",
        salt=ACME_SALT,
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
        api_key_sha256=sha256_hex(ACME_KEY),
        active_subscriber_base=125000,
    )
    beta = Tenant(
        code="BETA-ISP",
        name="Beta ISP",
        salt=BETA_SALT,
        profile_version=1,
        profile_fields={
            "subscriber_code": "required",
            "device_mac": "not_collected",
            "drm_session_id": "not_collected",
            "client_ip": "observed",
        },
        retention_days=30,
        api_key_sha256=sha256_hex(BETA_KEY),
        active_subscriber_base=40000,
    )
    db.add_all([acme, beta])
    await db.flush()
    tenants = {"ACME-MSO": acme, "BETA-ISP": beta}

    def user(username: str, password: str, role: str, tenant: Tenant | None = None, subscribers: bool = False) -> User:
        return User(
            username=username,
            password_hash=hash_password(password),
            role=role,
            tenant_id=tenant.id if tenant else None,
            subscriber_data_read=subscribers,
        )

    db.add_all(
        [
            user("admin", "admin123!", "super_admin", subscribers=True),
            user("noc", "noc123!", "noc"),
            user("acme", "acme123!", "tenant_user", acme),
            user("support", "support123!", "support", acme, subscribers=True),
            user("billing", "billing123!", "billing", acme),
        ]
    )
    for spec in NODES:
        db.add(
            Node(
                id=spec["id"],
                name=spec["name"],
                tier=spec["tier"],
                size_class=spec["size_class"],
                tenant_id=tenants[spec["tenant"]].id if spec.get("tenant") else None,
                region=spec["region"],
                city=spec["city"],
                asn=spec["asn"],
                base_url=spec["base_url"],
                priority=spec["priority"],
                nic_capacity_mbps=spec["nic"],
                desired_config_hash="demo-config",
                signing_secret=spec["secret"],
                simulate=spec["simulate"],
                enabled=True,
                status="offline",
            )
        )
    await db.flush()
    await import_channels(
        db,
        {
            "channels": [
                {"stream_uuid": uuid, "name": name, "code": code, "category": category}
                for uuid, name, code, category in CHANNELS
            ]
        },
        "seed",
    )

    nodes = {node.name: node for node in (await db.execute(select(Node))).scalars()}
    now = datetime.now(timezone.utc)
    for step in range(20, 0, -1):
        ts = now - timedelta(seconds=60 * step)
        for spec in NODES:
            if not spec["simulate"]:
                continue
            status, body = await push_sample(
                db,
                settings,
                nodes[spec["name"]],
                ts,
                interval=60,
                include_slow=step % 5 == 0,
            )
            if status >= 400:
                raise RuntimeError(f"seed telemetry failed for {spec['name']}: {status} {body}")
    for step in (2, 1):
        status, body = await push_sample(
            db,
            settings,
            nodes["edge-blr-01"],
            now - timedelta(seconds=60 * step),
            interval=60,
            include_slow=True,
        )
        if status >= 400:
            raise RuntimeError(f"seed telemetry failed for edge-blr-01: {status} {body}")

    hyd = nodes["edge-hyd-01"]
    resolved = demo_session(hyd, ACME_SALT, "HYD12345")
    status, body = await register_session(
        db,
        acme,
        {
            "tenant_code": "ACME-MSO",
            "session_handle": resolved["session_handle"],
            "subscriber_code": "HYD12345",
            "opened_at": resolved["started_at"],
            "expires_at": (now + timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
            "attributes": {
                "device_mac": "12:23:34:45:56:67",
                "drm_session_id": "wv-8f3a91c204e7",
                "stb_serial": "STB-AX-99120",
                "package_tier": "premium",
            },
        },
    )
    if status not in (200, 201):
        raise RuntimeError(f"seed registration failed: {status} {body}")
    unresolved = demo_session(hyd, ACME_SALT, "HYD99999")
    status, body = await push_sample(
        db,
        settings,
        hyd,
        now,
        interval=15,
        include_slow=False,
        sessions=[resolved, unresolved],
    )
    if status >= 400:
        raise RuntimeError(f"seed sessions failed: {status} {body}")

    programme_start = now - timedelta(minutes=30)
    db.add(
        EpgProgramme(
            tenant_id=acme.id,
            stream_uuid=CHANNELS[1][0],
            programme_ext_id="republic-prime",
            title="Prime Bulletin",
            genre="News",
            scheduled_start=programme_start,
            scheduled_end=programme_start + timedelta(hours=1),
            source="manual",
        )
    )
    db.add(
        SyslogMessage(
            node_id=hyd.id,
            severity="info",
            message=f"edge access sample stream={CHANNELS[0][0]} status=200 bytes=188000",
        )
    )
    db.add(
        SyslogMessage(
            node_id=nodes["edge-vja-01"].id,
            severity="warning",
            message="certificate expiry warning days_to_expiry=12",
        )
    )
    # Touch the unmapped constant so a future import does not silently reuse it.
    _ = UNMAPPED_STREAM
