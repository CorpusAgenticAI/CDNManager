"""Contract APIs used by nodes, middleware, and billing."""

import json
from datetime import datetime, timedelta, timezone

from app.security import ip_allowed, signature_ok, sign_url_token

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest import accept_telemetry
from app.models import Node, PurgeRequest, SteeringRule, SyslogMessage, Tenant, UsageBucket, UsagePeriod
from app.ops import (
    edge_map,
    import_channels,
    match_steering,
    origin_groups,
    parse_channel_csv,
    register_session,
    tenant_by_api_key,
)

router = APIRouter(prefix="/api/v1")


async def db_session(request: Request) -> AsyncSession:
    return request.state.db


async def bearer_tenant(request: Request, db: AsyncSession = Depends(db_session)) -> Tenant:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="bearer token required")
    tenant = await tenant_by_api_key(db, header.split(" ", 1)[1].strip())
    if tenant is None:
        raise HTTPException(status_code=401, detail="unknown API key")
    return tenant


@router.post("/telemetry")
async def telemetry(request: Request, db: AsyncSession = Depends(db_session)):
    raw = await request.body()
    client = request.client.host if request.client else ""
    status, body = await accept_telemetry(
        db,
        request.app.state.settings,
        raw=raw,
        headers={k: v for k, v in request.headers.items()},
        client_ip=client,
    )
    return JSONResponse(status_code=status, content=body)


@router.post("/sessions/register")
async def sessions_register(request: Request, tenant: Tenant = Depends(bearer_tenant), db: AsyncSession = Depends(db_session)):
    body = await request.json()
    status, payload = await register_session(db, tenant, body)
    return JSONResponse(status_code=status, content=payload)


@router.post("/sessions/register/batch")
async def sessions_register_batch(
    request: Request,
    tenant: Tenant = Depends(bearer_tenant),
    db: AsyncSession = Depends(db_session),
):
    body = await request.json()
    rows = body.get("sessions") if isinstance(body, dict) else None
    if not isinstance(rows, list) or len(rows) > 500:
        return JSONResponse(status_code=422, content={"error": "sessions must contain 1 to 500 objects"})
    results = []
    for row in rows:
        status, payload = await register_session(db, tenant, row)
        results.append({"status": status, **payload})
    return {"results": results}


@router.get("/edge-map")
async def get_edge_map(tenant_code: str, db: AsyncSession = Depends(db_session)):
    status, body = await edge_map(db, tenant_code)
    return JSONResponse(status_code=status, content=body)


@router.post("/channels/import")
async def channels_import(request: Request, tenant: Tenant = Depends(bearer_tenant), db: AsyncSession = Depends(db_session)):
    content_type = request.headers.get("content-type", "")
    raw = await request.body()
    if "text/csv" in content_type:
        parsed = parse_channel_csv(raw.decode())
    else:
        parsed = json.loads(raw)
    result = await import_channels(db, parsed, tenant.code)
    status = 422 if result.get("error") else 200
    return JSONResponse(status_code=status, content=result)


@router.post("/subscriber-base")
async def subscriber_base(request: Request, tenant: Tenant = Depends(bearer_tenant), db: AsyncSession = Depends(db_session)):
    body = await request.json()
    if body.get("tenant_code") != tenant.code:
        return JSONResponse(status_code=403, content={"error": "tenant_code does not match the API key"})
    try:
        tenant.active_subscriber_base = int(body["active_subscriber_base"])
    except Exception:
        return JSONResponse(status_code=422, content={"error": "active_subscriber_base is required"})
    if body.get("as_of"):
        tenant.subscriber_base_as_of = datetime.fromisoformat(str(body["as_of"]).replace("Z", "+00:00"))
    return {"tenant_code": tenant.code, "active_subscriber_base": tenant.active_subscriber_base}


@router.get("/usage")
async def usage(request: Request, tenant: Tenant = Depends(bearer_tenant), db: AsyncSession = Depends(db_session)):
    periods = await db.execute(
        select(UsagePeriod).where(UsagePeriod.tenant_id == tenant.id).order_by(UsagePeriod.period_start.desc()).limit(48)
    )
    buckets = await db.execute(
        select(UsageBucket).where(UsageBucket.tenant_id == tenant.id).order_by(UsageBucket.bucket_start.desc()).limit(36)
    )
    return {
        "tenant_code": tenant.code,
        "periods": [
            {
                "id": row.id,
                "period_start": row.period_start.isoformat(),
                "period_end": row.period_end.isoformat(),
                "body_bytes": row.body_bytes,
                "gap": row.gap,
                "status": row.status,
            }
            for row in periods.scalars()
        ],
        "open_buckets": [
            {
                "bucket_start": row.bucket_start.isoformat(),
                "body_bytes": row.body_bytes,
                "requests": row.requests,
                "gap": row.gap,
                "quality_hold": row.quality_hold,
            }
            for row in buckets.scalars()
        ],
    }


@router.post("/syslog")
async def syslog(request: Request, db: AsyncSession = Depends(db_session)):
    """Accept a forwarded access-log line. Forensic only — never a billing source."""
    raw = await request.body()
    node_id = request.headers.get("x-node-id", "")
    node = await db.get(Node, node_id) if node_id else None
    signature = request.headers.get("x-signature", "")
    if node is None or not signature_ok(node.signing_secret, raw, signature):
        raise HTTPException(status_code=401, detail="signature rejected")
    text = raw.decode(errors="replace")[:4000]
    severity = "warning" if " 5" in text or "error" in text.lower() else "info"
    db.add(SyslogMessage(node_id=node.id, message=text, severity=severity))
    return JSONResponse(status_code=202, content={"accepted": 1})


@router.get("/origin-map")
async def get_origin_map(db: AsyncSession = Depends(db_session)):
    """Active origin for each primary/secondary pair. Middleware caches this briefly."""
    return {"ttl_seconds": 30, "generated_at": datetime.now(timezone.utc).isoformat(), "groups": await origin_groups(db)}


@router.get("/steering")
async def get_steering(
    tenant_code: str,
    ip: str = "",
    region: str = "",
    asn: str = "",
    subscriber: str = "",
    db: AsyncSession = Depends(db_session),
):
    tenant = (await db.execute(select(Tenant).where(Tenant.code == tenant_code, Tenant.active.is_(True)))).scalar_one_or_none()
    if tenant is None:
        return JSONResponse(status_code=404, content={"error": "unknown tenant"})
    rules = list((await db.execute(select(SteeringRule).where(SteeringRule.tenant_id == tenant.id))).scalars())
    rule = match_steering(rules, ip=ip, region=region, asn=asn, subscriber=subscriber)
    if rule is None:
        return JSONResponse(status_code=404, content={"error": "no steering rule matched"})
    node = await db.get(Node, rule.node_id)
    return {
        "tenant_code": tenant.code,
        "match_type": rule.match_type,
        "edge": {"node_id": node.id, "name": node.name, "base_url": node.base_url, "region": node.region, "city": node.city},
    }


@router.post("/tokens")
async def issue_token(request: Request, tenant: Tenant = Depends(bearer_tenant)):
    """Signed playback token for SDP, CMS, or DRM. Rejected when the client IP is outside the allow list."""
    body = await request.json()
    client_ip = str(body.get("client_ip") or "")
    path = str(body.get("path") or "/")
    if not ip_allowed(tenant.ip_allowlist, client_ip):
        return JSONResponse(status_code=403, content={"error": "client IP is not allowed"})
    if not tenant.signed_urls:
        return {"signed": False, "path": path}
    expires = (datetime.now(timezone.utc) + timedelta(hours=4)).replace(microsecond=0).isoformat()
    token = sign_url_token(tenant.salt, path, expires, client_ip)
    return {"signed": True, "path": path, "expires": expires, "token": token, "client_ip": client_ip}


@router.post("/cache/purge")
async def api_purge(request: Request, tenant: Tenant = Depends(bearer_tenant), db: AsyncSession = Depends(db_session)):
    body = await request.json()
    scope = str(body.get("scope") or "")
    if scope not in {"file", "channel", "group", "cache"}:
        return JSONResponse(status_code=422, content={"error": "scope must be file, channel, group, or cache"})
    target = str(body.get("target") or "")
    if scope != "cache" and not target:
        return JSONResponse(status_code=422, content={"error": "target is required"})
    row = PurgeRequest(scope=scope, target=target, requested_by=tenant.code)
    db.add(row)
    await db.flush()
    return JSONResponse(status_code=202, content={"id": row.id, "scope": scope, "status": row.status})
