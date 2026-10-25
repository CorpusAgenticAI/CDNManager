# CDN Manager

Operations console and ingest service for a private CDN. Nodes push signed telemetry. Middleware registers opaque session handles. The NOC console shows health, cache efficiency, audience, and usage.

The stack matches the Xcoder console: Python 3.11+, FastAPI, Uvicorn, Jinja2, HTMX, Tailwind, Chart.js, SQLAlchemy, Alembic, Redis-backed login sessions, and bcrypt. PostgreSQL 16 is the production database. SQLite is the local preview so the console runs on this Windows machine without a server install.

## Run

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
cdnmanager
```

Open http://127.0.0.1:8080

| User | Password | Role |
| --- | --- | --- |
| admin | admin123! | Super admin, can resolve subscribers |
| noc | noc123! | NOC, aggregate and pseudonymous sessions |
| support | support123! | ACME support, can resolve ACME subscribers |
| acme | acme123! | ACME tenant user |
| billing | billing123! | ACME billing |

Tenant API keys for middleware: `acme-demo-key` and `beta-demo-key`.

A preview agent inside the process keeps the dashboard moving. Set `DEMO_TELEMETRY=false` to stop it. `scripts/simulate_node.py edge-hyd-01` posts one signed batch from outside the process.

Redis is used for login sessions when it is reachable. Otherwise sessions are stored in the application database.

## Production

Google Cloud hosting (Cloud Run, Cloud SQL for PostgreSQL 16, Secret Manager, Cloud Build) is described in [deploy/gcp/README.md](deploy/gcp/README.md).

`ENVIRONMENT=production` turns off the demo estate and the in-process agent, sets secure cookies and JSON logs, and stops the app from creating tables. Schema changes go through Alembic:

```bash
alembic upgrade head
```

After changing `app/models.py`, add a migration with `alembic revision --autogenerate -m "what changed"`. `tests/test_production.py` fails if the models and migrations disagree.

On a large estate the metric and viewing-session tables are the ones to move onto TimescaleDB hypertables; the columns are already the contract's columns.

## What the contract requires of this service

- `POST /api/v1/telemetry` checks the HMAC of the raw body before it decompresses, rejects an unknown schema major version, rejects a tier/group mismatch, treats a repeated batch as success, and does not invent a delta across a counter-epoch change.
- `POST /api/v1/sessions/register` (and `/batch`) stores only the attributes in that tenant's profile and says which ones it dropped.
- `GET /api/v1/edge-map` returns the tenant's edges with a 60-second TTL.
- `POST /api/v1/channels/import` refuses to reassign a retired stream UUID.
- Usage is body bytes on 5-minute buckets. A sequence gap flags the bucket. An adjustment is a new row, not an edit.
- Subscriber identity is resolved only with `subscriber_data:read`, and only inside the caller's tenant. Every resolution is audited.
- Programme audience is the time overlap of a session and a programme, labelled as-run or as-published.
- Syslog is a 14-day forensic store. It is not a billing source.

`pytest` covers those rules.
