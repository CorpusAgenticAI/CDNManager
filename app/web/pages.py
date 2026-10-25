"""NOC console. Server-rendered pages with HTMX updates."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
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
    PrepositionJob,
    PurgeRequest,
    SessionRegistration,
    SteeringRule,
    StreamSighting,
    SyslogMessage,
    Tenant,
    UsageAdjustment,
    UsageBucket,
    UsagePeriod,
    User,
    ViewingSession,
    Webhook,
)
from app.ops import (
    audit,
    capacity_rows,
    dashboard_kpis,
    finalize_periods,
    import_channels,
    origin_groups,
    pair_origins,
    programme_audience,
    resolve_session,
    scoped_tenant,
    series,
    top_channels,
)
from app.security import COOKIE, hash_password, new_token, verify_password
from app.web.deps import csrf_ok, db_session, load_user

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _bytes(value) -> str:
    if value is None:
        return "—"
    number = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(number) < 1024:
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} PB"


templates.env.filters["bytes"] = _bytes
templates.env.filters["when"] = lambda value: value.strftime("%Y-%m-%d %H:%M:%S") if value else "—"


def _nav(user: User) -> list[tuple[str, str]]:
    items = [("/", "Overview"), ("/nodes", "Nodes"), ("/origins", "Origins"), ("/channels", "Channels")]
    if user.role != "billing":
        items.extend([("/steering", "Steering"), ("/cache", "Cache"), ("/capacity", "Capacity")])
    items.append(("/alerts", "Alerts"))
    if user.role != "billing":
        items.append(("/sessions", "Sessions"))
    items.append(("/reports", "Reports"))
    if user.role in {"super_admin", "noc", "billing", "tenant_user", "support"}:
        items.append(("/billing", "Billing"))
    if user.role != "billing":
        items.extend([("/epg", "Programmes"), ("/syslog", "Syslog")])
    if user.role in {"super_admin", "noc"}:
        items.append(("/audit", "Audit"))
    if user.role == "super_admin":
        items.extend([("/tenants", "Tenants"), ("/users", "Users")])
    return items


async def guard(request: Request, db: AsyncSession, roles: set[str] | None = None):
    user, login = await load_user(request, db)
    if user is None or login is None:
        return None
    if roles is not None and user.role not in roles and user.role != "super_admin":
        return "deny"
    return user, login


def render(request: Request, name: str, user: User, login, **extra):
    return templates.TemplateResponse(
        request,
        name,
        {
            "user": user,
            "csrf": login.csrf_token,
            "nav": _nav(user),
            "session_backend": request.app.state.store.backend,
            **extra,
        },
    )


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    demo = request.app.state.settings.seed_demo
    return templates.TemplateResponse(request, "login.html", {"error": "", "demo_accounts": demo})


@router.post("/login")
async def login(request: Request, username: str = Form(), password: str = Form(), db: AsyncSession = Depends(db_session)):
    result = await db.execute(select(User).where(User.username == username.strip()))
    user = result.scalar_one_or_none()
    if user is None or not user.active or not verify_password(password, user.password_hash):
        demo = request.app.state.settings.seed_demo
        return templates.TemplateResponse(
            request, "login.html", {"error": "Unknown username or password", "demo_accounts": demo}, status_code=401
        )
    row = await request.app.state.store.put(db, user.id)
    await audit(db, user.username, "login", {})
    response = RedirectResponse("/", status_code=303)
    settings = request.app.state.settings
    response.set_cookie(
        COOKIE,
        row.token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.session_ttl_hours * 3600,
    )
    return response


@router.post("/logout")
async def logout(request: Request, db: AsyncSession = Depends(db_session)):
    token = request.cookies.get(COOKIE)
    if token:
        await request.app.state.store.delete(db, token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, httponly=True, samesite="lax", secure=request.app.state.settings.cookie_secure)
    return response


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db)
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    scope = scoped_tenant(user)
    kpis = await dashboard_kpis(db, scope)
    channels = await top_channels(db, 10)
    alerts = await db.execute(select(Alert).where(Alert.closed_at.is_(None)).order_by(Alert.opened_at.desc()).limit(8))
    return render(
        request,
        "dashboard.html",
        user,
        login,
        kpis=kpis,
        channels=channels,
        alerts=list(alerts.scalars()),
    )


@router.get("/partials/kpis", response_class=HTMLResponse)
async def kpi_partial(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db)
    if not isinstance(auth, tuple):
        return HTMLResponse("", status_code=401)
    user, login = auth
    kpis = await dashboard_kpis(db, scoped_tenant(user))
    return render(request, "_kpis.html", user, login, kpis=kpis)


@router.get("/ui/series")
async def ui_series(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db)
    if not isinstance(auth, tuple):
        return JSONResponse({"error": "login required"}, status_code=401)
    return await series(db)


@router.get("/nodes", response_class=HTMLResponse)
async def nodes(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db, {"noc", "tenant_user", "support", "billing"})
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    if auth == "deny":
        return HTMLResponse("Not allowed", status_code=403)
    user, login = auth
    query = select(Node).order_by(Node.tier, Node.name)
    scope = scoped_tenant(user)
    if scope:
        query = query.where(Node.tenant_id == scope)
    rows = list((await db.execute(query)).scalars())
    tenants = list((await db.execute(select(Tenant).order_by(Tenant.code))).scalars())
    return render(request, "nodes.html", user, login, nodes=rows, tenants=tenants)


@router.post("/nodes")
async def create_node(
    request: Request,
    db: AsyncSession = Depends(db_session),
    name: str = Form(),
    tier: str = Form(),
    region: str = Form(""),
    city: str = Form(""),
    base_url: str = Form(""),
    tenant_id: str = Form(""),
    size_class: str = Form("M"),
    csrf: str = Form(""),
):
    auth = await guard(request, db, {"noc"})
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    if tier not in {"edge", "mid", "origin"}:
        return HTMLResponse("Unknown tier", status_code=422)
    secret = new_token()
    node = Node(
        name=name.strip(),
        tier=tier,
        region=region,
        city=city,
        base_url=base_url,
        tenant_id=tenant_id or None,
        size_class=size_class,
        signing_secret=secret,
        desired_config_hash="demo-config",
    )
    db.add(node)
    await audit(db, user.username, "node.create", {"name": node.name, "tier": tier})
    await db.flush()
    return RedirectResponse(f"/nodes/{node.id}?secret=1", status_code=303)


@router.get("/nodes/{node_id}", response_class=HTMLResponse)
async def node_detail(node_id: str, request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db, {"noc", "tenant_user", "support", "billing"})
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    if auth == "deny":
        return HTMLResponse("Not allowed", status_code=403)
    user, login = auth
    node = await db.get(Node, node_id)
    if node is None:
        return HTMLResponse("Node not found", status_code=404)
    scope = scoped_tenant(user)
    if scope and node.tenant_id != scope:
        return HTMLResponse("Node not found", status_code=404)
    samples = list(
        (
            await db.execute(
                select(MetricSample).where(MetricSample.node_id == node.id).order_by(MetricSample.ts.desc()).limit(20)
            )
        ).scalars()
    )
    alerts = list(
        (await db.execute(select(Alert).where(Alert.node_id == node.id).order_by(Alert.opened_at.desc()).limit(10))).scalars()
    )
    show_secret = request.query_params.get("secret") == "1" and user.role == "super_admin"
    return render(
        request,
        "node.html",
        user,
        login,
        node=node,
        samples=samples,
        alerts=alerts,
        show_secret=show_secret,
    )


@router.post("/nodes/{node_id}/toggle")
async def toggle_node(node_id: str, request: Request, db: AsyncSession = Depends(db_session), csrf: str = Form("")):
    auth = await guard(request, db, {"noc"})
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    node = await db.get(Node, node_id)
    if node is None:
        return HTMLResponse("Node not found", status_code=404)
    node.enabled = not node.enabled
    node.status = "disabled" if not node.enabled else node.status
    await audit(db, user.username, "node.toggle", {"name": node.name, "enabled": node.enabled})
    return RedirectResponse(f"/nodes/{node.id}", status_code=303)


@router.get("/ui/nodes/{node_id}/series")
async def node_series(node_id: str, request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db)
    if not isinstance(auth, tuple):
        return JSONResponse({"error": "login required"}, status_code=401)
    since = datetime.now(timezone.utc) - timedelta(minutes=60)
    rows = list(
        (
            await db.execute(
                select(MetricSample).where(MetricSample.node_id == node_id, MetricSample.ts >= since).order_by(MetricSample.ts)
            )
        ).scalars()
    )
    return {
        "labels": [row.ts.strftime("%H:%M:%S") for row in rows],
        "cpu": [row.cpu_pct for row in rows],
        "mbps": [
            round(((row.d_tx_bytes or 0) * 8 / row.interval_s / 1_000_000), 2) if row.interval_s else 0 for row in rows
        ],
    }


@router.get("/channels", response_class=HTMLResponse)
async def channels(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db)
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    rows = list((await db.execute(select(Channel).order_by(Channel.name))).scalars())
    sightings = list((await db.execute(select(StreamSighting).order_by(StreamSighting.bytes_sent.desc()))).scalars())
    known = {row.stream_uuid for row in rows}
    unmapped = [row for row in sightings if row.stream_uuid not in known]
    idle = [row for row in rows if row.last_seen_at is None and row.retired_at is None]
    return render(request, "channels.html", user, login, channels=rows, unmapped=unmapped, idle=idle)


@router.post("/channels/import")
async def channels_form(
    request: Request,
    db: AsyncSession = Depends(db_session),
    stream_uuid: str = Form(),
    name: str = Form(),
    code: str = Form(""),
    category: str = Form(""),
    protocol: str = Form("hls"),
    kind: str = Form("live"),
    csrf: str = Form(""),
):
    auth = await guard(request, db, {"noc"})
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    await import_channels(
        db,
        {
            "channels": [
                {
                    "stream_uuid": stream_uuid.strip(),
                    "name": name.strip(),
                    "code": code,
                    "category": category,
                    "protocol": protocol,
                    "kind": kind,
                }
            ]
        },
        user.username,
    )
    return RedirectResponse("/channels", status_code=303)


@router.post("/channels/{channel_id}/retire")
async def retire_channel(channel_id: str, request: Request, db: AsyncSession = Depends(db_session), csrf: str = Form("")):
    auth = await guard(request, db, {"noc"})
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    channel = await db.get(Channel, channel_id)
    if channel is None:
        return HTMLResponse("Channel not found", status_code=404)
    channel.retired_at = datetime.now(timezone.utc)
    await audit(db, user.username, "channel.retire", {"stream_uuid": channel.stream_uuid})
    return RedirectResponse("/channels", status_code=303)


@router.get("/origins", response_class=HTMLResponse)
async def origins(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db)
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    rows = list((await db.execute(select(Node).where(Node.tier == "origin").order_by(Node.name))).scalars())
    groups = await origin_groups(db)
    return render(request, "origins.html", user, login, origins=rows, groups=groups)


@router.post("/origins/pair")
async def origins_pair(
    request: Request,
    db: AsyncSession = Depends(db_session),
    primary_id: str = Form(),
    secondary_id: str = Form(),
    csrf: str = Form(""),
):
    auth = await guard(request, db, {"noc"})
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    status, message = await pair_origins(db, primary_id, secondary_id, user.username)
    if status != 200:
        return HTMLResponse(message, status_code=status)
    return RedirectResponse("/origins", status_code=303)


@router.get("/steering", response_class=HTMLResponse)
async def steering(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db, {"noc", "tenant_user", "support"})
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    if auth == "deny":
        return HTMLResponse("Not allowed", status_code=403)
    user, login = auth
    query = select(SteeringRule).order_by(SteeringRule.priority, SteeringRule.created_at)
    scope = scoped_tenant(user)
    if scope:
        query = query.where(SteeringRule.tenant_id == scope)
    rules = list((await db.execute(query)).scalars())
    edges = list((await db.execute(select(Node).where(Node.tier == "edge").order_by(Node.name))).scalars())
    if scope:
        edges = [node for node in edges if node.tenant_id == scope]
    tenants = list((await db.execute(select(Tenant).order_by(Tenant.code))).scalars())
    nodes = {node.id: node.name for node in (await db.execute(select(Node))).scalars()}
    return render(request, "steering.html", user, login, rules=rules, edges=edges, tenants=tenants, nodes=nodes)


@router.post("/steering")
async def steering_add(
    request: Request,
    db: AsyncSession = Depends(db_session),
    match_type: str = Form(),
    match_value: str = Form(),
    node_id: str = Form(),
    tenant_id: str = Form(""),
    priority: int = Form(100),
    csrf: str = Form(""),
):
    auth = await guard(request, db, {"noc"})
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    if match_type not in {"ip_cidr", "region", "asn", "subscriber_prefix"}:
        return HTMLResponse("Unknown match type", status_code=422)
    edge = await db.get(Node, node_id)
    if edge is None or edge.tier != "edge":
        return HTMLResponse("Choose an edge", status_code=422)
    db.add(
        SteeringRule(
            tenant_id=tenant_id or None,
            match_type=match_type,
            match_value=match_value.strip(),
            node_id=edge.id,
            priority=priority,
        )
    )
    await audit(db, user.username, "steering.create", {"match_type": match_type, "match_value": match_value, "node": edge.name})
    return RedirectResponse("/steering", status_code=303)


@router.get("/cache", response_class=HTMLResponse)
async def cache_ops(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db, {"noc", "tenant_user", "support"})
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    if auth == "deny":
        return HTMLResponse("Not allowed", status_code=403)
    user, login = auth
    purges = list((await db.execute(select(PurgeRequest).order_by(PurgeRequest.created_at.desc()).limit(20))).scalars())
    jobs = list((await db.execute(select(PrepositionJob).order_by(PrepositionJob.created_at.desc()).limit(20))).scalars())
    edges = list((await db.execute(select(Node).where(Node.tier == "edge", Node.enabled.is_(True)).order_by(Node.name))).scalars())
    channels = list((await db.execute(select(Channel).where(Channel.retired_at.is_(None)).order_by(Channel.name))).scalars())
    nodes = {node.id: node.name for node in (await db.execute(select(Node))).scalars()}
    popular = await top_channels(db, 5)
    return render(
        request,
        "cache.html",
        user,
        login,
        purges=purges,
        jobs=jobs,
        edges=edges,
        channels=channels,
        nodes=nodes,
        popular=popular,
    )


@router.post("/cache/purge")
async def cache_purge(
    request: Request,
    db: AsyncSession = Depends(db_session),
    scope: str = Form(),
    target: str = Form(""),
    node_id: str = Form(""),
    csrf: str = Form(""),
):
    auth = await guard(request, db, {"noc"})
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    if scope not in {"file", "channel", "group", "cache"}:
        return HTMLResponse("Unknown purge scope", status_code=422)
    if scope != "cache" and not target.strip():
        return HTMLResponse("Name the file, channel, or group to purge", status_code=422)
    db.add(PurgeRequest(scope=scope, target=target.strip(), node_id=node_id or None, requested_by=user.username))
    await audit(db, user.username, "cache.purge", {"scope": scope, "target": target, "node_id": node_id or "all"})
    return RedirectResponse("/cache", status_code=303)


@router.post("/cache/preposition")
async def cache_preposition(
    request: Request,
    db: AsyncSession = Depends(db_session),
    stream_uuid: str = Form(),
    node_id: str = Form(),
    csrf: str = Form(""),
):
    auth = await guard(request, db, {"noc"})
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    edge = await db.get(Node, node_id)
    if edge is None or edge.tier != "edge":
        return HTMLResponse("Choose an edge", status_code=422)
    db.add(PrepositionJob(node_id=edge.id, stream_uuid=stream_uuid.strip(), requested_by=user.username))
    await audit(db, user.username, "cache.preposition", {"stream_uuid": stream_uuid, "node": edge.name})
    return RedirectResponse("/cache", status_code=303)


@router.get("/capacity", response_class=HTMLResponse)
async def capacity(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db, {"noc", "tenant_user", "support"})
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    if auth == "deny":
        return HTMLResponse("Not allowed", status_code=403)
    user, login = auth
    rows = await capacity_rows(db, scoped_tenant(user))
    return render(request, "capacity.html", user, login, rows=rows)


@router.get("/alerts", response_class=HTMLResponse)
async def alerts(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db)
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    rows = list((await db.execute(select(Alert).order_by(Alert.opened_at.desc()).limit(100))).scalars())
    scope = scoped_tenant(user)
    if scope:
        rows = [row for row in rows if row.tenant_id in (None, scope)]
    nodes = {node.id: node for node in (await db.execute(select(Node))).scalars()}
    return render(request, "alerts.html", user, login, alerts=rows, nodes=nodes)


@router.post("/alerts/{alert_id}/ack")
async def ack_alert(alert_id: str, request: Request, db: AsyncSession = Depends(db_session), csrf: str = Form("")):
    auth = await guard(request, db, {"noc", "tenant_user", "support"})
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    alert = await db.get(Alert, alert_id)
    if alert is None:
        return HTMLResponse("Alert not found", status_code=404)
    alert.acked_by = user.username
    alert.acked_at = datetime.now(timezone.utc)
    await audit(db, user.username, "alert.ack", {"alert_id": alert.id, "type": alert.type})
    return RedirectResponse("/alerts", status_code=303)


@router.get("/sessions", response_class=HTMLResponse)
async def sessions(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db, {"noc", "tenant_user", "support"})
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    if auth == "deny":
        return HTMLResponse("Not allowed", status_code=403)
    user, login = auth
    query = select(ViewingSession).order_by(ViewingSession.ended_at.desc()).limit(100)
    scope = scoped_tenant(user)
    if scope:
        query = query.where(ViewingSession.tenant_id == scope)
    rows = list((await db.execute(query)).scalars())
    channels = {row.stream_uuid: row.name for row in (await db.execute(select(Channel))).scalars()}
    nodes = {node.id: node.name for node in (await db.execute(select(Node))).scalars()}
    handles = [row.session_handle for row in rows if row.session_handle]
    registered = set()
    if handles:
        found = await db.execute(select(SessionRegistration.session_handle).where(SessionRegistration.session_handle.in_(handles)))
        registered = set(found.scalars())
    return render(
        request,
        "sessions.html",
        user,
        login,
        sessions=rows,
        channels=channels,
        nodes=nodes,
        registered=registered,
    )


@router.post("/sessions/{session_id}/resolve", response_class=HTMLResponse)
async def resolve(session_id: str, request: Request, db: AsyncSession = Depends(db_session), csrf: str = Form("")):
    auth = await guard(request, db, {"noc", "tenant_user", "support"})
    if not isinstance(auth, tuple):
        return HTMLResponse("Login required", status_code=401)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    status, body = await resolve_session(db, user, session_id)
    if status == 403:
        return HTMLResponse("subscriber_data:read is required to see a subscriber code.", status_code=403)
    if status == 404:
        return HTMLResponse("Session not found", status_code=404)
    resolved = body.get("resolved")
    if not resolved:
        return HTMLResponse("Unresolved — no directory entry for this handle.")
    bits = " · ".join(f"{key}: {value}" for key, value in resolved.items())
    return HTMLResponse(bits)


@router.get("/reports", response_class=HTMLResponse)
async def reports(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db)
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    kpis = await dashboard_kpis(db, scoped_tenant(user))
    channels = await top_channels(db, 10)
    regions = await db.execute(select(Node.region, func.count()).group_by(Node.region))
    return render(request, "reports.html", user, login, kpis=kpis, channels=channels, regions=list(regions))


@router.get("/billing", response_class=HTMLResponse)
async def billing(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db)
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    scope = scoped_tenant(user)
    period_query = select(UsagePeriod).order_by(UsagePeriod.period_start.desc()).limit(48)
    bucket_query = select(UsageBucket).order_by(UsageBucket.bucket_start.desc()).limit(48)
    if scope:
        period_query = period_query.where(UsagePeriod.tenant_id == scope)
        bucket_query = bucket_query.where(UsageBucket.tenant_id == scope)
    periods = list((await db.execute(period_query)).scalars())
    buckets = list((await db.execute(bucket_query)).scalars())
    adjustments = list((await db.execute(select(UsageAdjustment).order_by(UsageAdjustment.created_at.desc()).limit(20))).scalars())
    tenants = {row.id: row.code for row in (await db.execute(select(Tenant))).scalars()}
    return render(
        request,
        "billing.html",
        user,
        login,
        periods=periods,
        buckets=buckets,
        adjustments=adjustments,
        tenants=tenants,
    )


@router.post("/billing/finalize")
async def billing_finalize(request: Request, db: AsyncSession = Depends(db_session), csrf: str = Form("")):
    auth = await guard(request, db, {"noc", "billing"})
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    created = await finalize_periods(db, datetime.now(timezone.utc) + timedelta(hours=2))
    await audit(db, user.username, "usage.finalise", {"count": len(created)})
    return RedirectResponse("/billing", status_code=303)


@router.post("/billing/{period_id}/adjust")
async def adjust(
    period_id: str,
    request: Request,
    db: AsyncSession = Depends(db_session),
    body_bytes_delta: int = Form(),
    reason: str = Form(),
    csrf: str = Form(""),
):
    auth = await guard(request, db, {"noc", "billing"})
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    period = await db.get(UsagePeriod, period_id)
    if period is None:
        return HTMLResponse("Period not found", status_code=404)
    db.add(
        UsageAdjustment(
            period_id=period.id,
            body_bytes_delta=body_bytes_delta,
            reason=reason,
            created_by=user.username,
        )
    )
    await audit(db, user.username, "usage.adjust", {"period_id": period.id, "delta": body_bytes_delta})
    return RedirectResponse("/billing", status_code=303)


@router.get("/epg", response_class=HTMLResponse)
async def epg(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db, {"noc", "tenant_user", "support"})
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    if auth == "deny":
        return HTMLResponse("Not allowed", status_code=403)
    user, login = auth
    query = select(EpgProgramme).order_by(EpgProgramme.scheduled_start.desc()).limit(50)
    scope = scoped_tenant(user)
    if scope:
        query = query.where(EpgProgramme.tenant_id == scope)
    programmes = []
    for programme in (await db.execute(query)).scalars():
        programmes.append(await programme_audience(db, programme))
    channels = list((await db.execute(select(Channel).where(Channel.retired_at.is_(None)).order_by(Channel.name))).scalars())
    return render(request, "epg.html", user, login, programmes=programmes, channels=channels)


@router.post("/epg")
async def add_epg(
    request: Request,
    db: AsyncSession = Depends(db_session),
    stream_uuid: str = Form(),
    title: str = Form(),
    programme_ext_id: str = Form(),
    start: str = Form(),
    end: str = Form(),
    genre: str = Form(""),
    csrf: str = Form(""),
):
    auth = await guard(request, db, {"noc"})
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    db.add(
        EpgProgramme(
            stream_uuid=stream_uuid,
            title=title,
            programme_ext_id=programme_ext_id,
            genre=genre,
            scheduled_start=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
            scheduled_end=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
            source="manual",
        )
    )
    db.add(EpgChannelMap(epg_channel_id=programme_ext_id, stream_uuid=stream_uuid))
    return RedirectResponse("/epg", status_code=303)


@router.get("/syslog", response_class=HTMLResponse)
async def syslog_page(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db, {"noc", "tenant_user", "support"})
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    if auth == "deny":
        return HTMLResponse("Not allowed", status_code=403)
    user, login = auth
    rows = list((await db.execute(select(SyslogMessage).order_by(SyslogMessage.received_at.desc()).limit(50))).scalars())
    return render(request, "syslog.html", user, login, messages=rows)


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db, {"noc"})
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    if auth == "deny":
        return HTMLResponse("Not allowed", status_code=403)
    user, login = auth
    rows = list((await db.execute(select(AuditLog).order_by(AuditLog.at.desc()).limit(100))).scalars())
    return render(request, "audit.html", user, login, rows=rows)


@router.get("/tenants", response_class=HTMLResponse)
async def tenants(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db, set())
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    if auth == "deny":
        return HTMLResponse("Not allowed", status_code=403)
    user, login = auth
    rows = list((await db.execute(select(Tenant).order_by(Tenant.code))).scalars())
    return render(request, "tenants.html", user, login, tenants=rows)


@router.post("/tenants/{tenant_id}/salt")
async def rotate_salt(tenant_id: str, request: Request, db: AsyncSession = Depends(db_session), csrf: str = Form("")):
    auth = await guard(request, db, set())
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        return HTMLResponse("Tenant not found", status_code=404)
    tenant.salt = new_token()
    tenant.profile_version += 1
    await audit(db, user.username, "tenant.salt_rotate", {"tenant": tenant.code})
    return RedirectResponse("/tenants", status_code=303)


@router.post("/tenants/{tenant_id}/security")
async def tenant_security(
    tenant_id: str,
    request: Request,
    db: AsyncSession = Depends(db_session),
    ip_allowlist: str = Form(""),
    signed_urls: str = Form(""),
    csrf: str = Form(""),
):
    auth = await guard(request, db, set())
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        return HTMLResponse("Tenant not found", status_code=404)
    tenant.signed_urls = signed_urls == "on"
    tenant.ip_allowlist = ip_allowlist.strip()
    await audit(db, user.username, "tenant.security", {"tenant": tenant.code, "signed_urls": tenant.signed_urls})
    return RedirectResponse("/tenants", status_code=303)


@router.get("/users", response_class=HTMLResponse)
async def users(request: Request, db: AsyncSession = Depends(db_session)):
    auth = await guard(request, db, set())
    if auth is None:
        return RedirectResponse("/login", status_code=303)
    if auth == "deny":
        return HTMLResponse("Not allowed", status_code=403)
    user, login = auth
    rows = list((await db.execute(select(User).order_by(User.username))).scalars())
    tenants = list((await db.execute(select(Tenant))).scalars())
    return render(request, "users.html", user, login, users=rows, tenants=tenants)


@router.post("/users")
async def create_user(
    request: Request,
    db: AsyncSession = Depends(db_session),
    username: str = Form(),
    password: str = Form(),
    role: str = Form(),
    tenant_id: str = Form(""),
    subscriber_data_read: str = Form(""),
    csrf: str = Form(""),
):
    auth = await guard(request, db, set())
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    if role not in {"super_admin", "noc", "tenant_user", "support", "billing"}:
        return HTMLResponse("Unknown role", status_code=422)
    db.add(
        User(
            username=username.strip(),
            password_hash=hash_password(password),
            role=role,
            tenant_id=tenant_id or None,
            subscriber_data_read=subscriber_data_read == "on",
        )
    )
    await audit(db, user.username, "user.create", {"username": username, "role": role})
    return RedirectResponse("/users", status_code=303)


@router.post("/webhooks")
async def add_webhook(
    request: Request,
    db: AsyncSession = Depends(db_session),
    url: str = Form(),
    csrf: str = Form(""),
):
    auth = await guard(request, db, set())
    if not isinstance(auth, tuple):
        return RedirectResponse("/login", status_code=303)
    user, login = auth
    if not csrf_ok(request, login, {"csrf": csrf}):
        return HTMLResponse("CSRF check failed", status_code=403)
    db.add(Webhook(url=url, secret=new_token(), event_types=["alert.opened", "usage.period_finalised"]))
    await audit(db, user.username, "webhook.create", {"url": url})
    return RedirectResponse("/audit", status_code=303)
