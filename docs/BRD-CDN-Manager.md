# Business Requirements Document

## CDN Manager

| | |
| --- | --- |
| Document ID | BRD-CDN-001 |
| Version | 1.0 |
| Date | 8 October 2026 |
| Status | Updated for business review |
| Product | CDN Manager — operations console, telemetry ingest, and viewership |
| Audience | Product owner, NOC, franchisee operations, billing, privacy, and delivery partners |
| Source | High-level business requirements for CDN Manager, plus the operating contract already enforced by the service |

This document states what the business needs CDN Manager to do. Version 1.0 adds origin failover, content delivery type, subscriber and network steering, cache pre-position and purge, capacity planning, signed playback tokens, and the NOC KPI set. The earlier contract remains in force: signed node telemetry, tenant-scoped sessions, and usage that can be billed without rewriting history.

---

## 1. Purpose

A private CDN serves more than one franchisee from shared edge, mid-tier, and origin nodes, including nodes in the operator’s own network and in partner ISP networks. The operator needs one place to answer six questions:

1. Is the estate healthy, and which edge or origin is the problem?
2. If an origin fails, which origin should carry the traffic next?
3. Which edge should serve a subscriber, IP range, region, or network?
4. What is being watched, and is popular content cached close to those subscribers?
5. Where is capacity running out, and what should be filled or purged before the peak?
6. How much traffic did each tenant actually deliver, without exposing a subscriber code to people who do not need it?

CDN Manager is that place. Nodes push signed measurements. Middleware registers opaque session handles, reads the edge map and the origin map, and asks which edge a viewer should use. Operators use the console. Billing systems read finalised usage.

## 2. Business objectives

| ID | Objective | How success is recognised |
| --- | --- | --- |
| BO-1 | Run the CDN from one console | An operator sees estate health, node status, alerts, regional traffic, and the top 10 channels without logging into each node. |
| BO-2 | Keep service up when an origin fails | A primary origin has a secondary. When the primary is unavailable, the origin map names the secondary and says traffic was redirected. |
| BO-3 | Place viewers on the right edge | A subscriber prefix, IP range, region, or network maps to one edge for that franchisee. |
| BO-4 | Use cache instead of the origin | Cache hit ratio and bandwidth saved are visible. Popular titles can be pre-positioned. Stale objects can be purged. |
| BO-5 | See where capacity is short | Current, peak, and projected load are compared with link size. A location that is tight is marked as needing capacity. |
| BO-6 | Bill delivered bytes fairly | Usage is body bytes, bucketed, and finalised. Gaps and bad counters are flagged. Corrections are new rows, never silent edits. |
| BO-7 | Keep franchisees isolated | A tenant user sees only that tenant’s sessions, steering, capacity, programmes, and usage. |
| BO-8 | Protect subscriber identity | Viewing data stays pseudonymous. A subscriber code is shown only to a permitted person, inside that tenant, and every reveal is audited. Playback can require a signed token, and can be limited to an IP allow list. |

## 3. Scope

### 3.1 In scope

- Operator login and role-based console for administrators, NOC, customers, and support.
- Tenant administration, user administration, tenant API keys, signed-URL policy, and IP allow lists.
- Node registry for edges in own and partner networks, plus mid-tier and origin servers. Edges can be added, configured, and disabled.
- Primary and secondary origin pairs, and an origin map that names the origin to use when the primary has failed.
- Live TV and VOD catalogue entries delivered as HLS, DASH, or both.
- Signed telemetry ingest and remote monitoring of traffic, bandwidth, cache hit and miss, concurrent sessions, CPU, memory, storage, and node health.
- Alerts for edge failure, high bandwidth, storage exhaustion, abnormal traffic, origin failure, and service degradation.
- Steering of subscribers, IP ranges, regions, and networks onto an edge.
- Cache pre-position of a title onto a selected edge, and purge of a file, channel, content group, or a complete cache.
- Capacity view: current utilisation, projected load, and locations that need more capacity.
- The NOC KPI set in section 7.4.
- Session registration, programme audience, and reports.
- Usage buckets, hourly periods, adjustments, and a usage API for billing.
- REST APIs for middleware, subscriber systems, billing, CMS, and orchestration, as listed in section 7.11.
- Forensic syslog, separate from billing.
- Audit of configuration changes, user activity, cache operations, node changes, and subscriber resolution.
- Retention: subscriber directory by tenant policy, viewing-handle redaction, and a 14-day syslog window.

### 3.2 Out of scope

- Encoding, packaging, the DRM licence server, and the player itself. CDN Manager issues a signed playback token; it does not license content.
- Moving packets. The service publishes the edge map, the origin map, and the steering decision. DNS, anycast, and the player follow that decision.
- Applying a purge or a pre-position fill on the node. The console records and audits the instruction. The node agent carries it out.
- Invoicing, tax, collections, and the general ledger. CDN Manager supplies the usage figure and the audit trail.
- Using syslog, access logs, or estimated rates as a billing source.
- Storing subscriber names, addresses, or payment data.
- Multi-currency pricing, rate cards, and discounts.

## 4. Stakeholders and actors

| Actor | Interest |
| --- | --- |
| Platform super administrator | Owns tenants, users, access policy, node secrets, and salt rotation. Can be granted subscriber resolution across the estate. |
| NOC operator | Watches health, pairs origins, steers viewers, requests cache fill and purge, acknowledges alerts, maintains the programme guide, and can finalise or adjust usage. |
| Tenant user | Franchisee staff. Sees that tenant’s overview, sessions, steering, capacity, reports, programmes, syslog, and billing. Cannot resolve a subscriber unless separately permitted. |
| Tenant support | Same tenant boundary as a tenant user, typically with permission to resolve a subscriber for a support case. |
| Tenant billing user | Sees and can finalise or adjust that tenant’s usage. Does not see sessions, programmes, syslog, steering, cache, or capacity. |
| CDN node agent | Edge, mid, or origin process that signs and posts telemetry, and applies cache instructions. |
| Middleware and SDP | Registers session handles, reads the edge map and origin map, resolves steering, and requests signed playback tokens. |
| CMS and orchestration | Imports channels and requests a purge. |
| Downstream billing or NOC system | Receives `usage.period_finalised` and `alert.opened` webhooks, or polls the usage API. |

Demo identities used to exercise these roles: `admin` (super administrator), `noc`, `acme` (tenant user), `support`, and `billing`.

## 5. Business context

Two example franchisees illustrate the operating model:

| Tenant | Code | What it is allowed to store about a session |
| --- | --- | --- |
| ACME MSO | ACME-MSO | Subscriber code, device MAC, and DRM session are required. Set-top serial and package tier are optional. Client IP is observed on the node and is not kept in the directory. Directory retention is 90 days. Subscriber base 125,000. |
| Beta ISP | BETA-ISP | Subscriber code is required. Device MAC and DRM session must not be collected. Directory retention is 30 days. Subscriber base 40,000. |

Each tenant has its own salt. The same subscriber code produces a different pseudonym in each tenant. Nodes never send a subscriber code. Middleware does, and only inside a handle the service can check against that salt.

The preview estate pairs `origin-a` as primary with `origin-b` as secondary. ACME South and the lab range `203.0.113.0/24` steer to `edge-hyd-01`. Beta South and network 45820 steer to `edge-vja-01`.

## 6. User journeys

### 6.1 NOC shift

1. The operator signs in and lands on the overview. The tiles are the figures to quote: cache hit ratio, bandwidth saved percent, total CDN traffic, origin traffic, peak rate, concurrent streams, average response time, edge availability, origin availability, and CPU, memory, and storage. Traffic by region and the top 10 channels sit with the open alerts.
2. The operator opens a node that is offline or hot, confirms it is enabled, and reads recent samples.
3. The operator opens Origins. If the primary has failed, the active origin is the secondary and the status says redirected.
4. The operator acknowledges an alert only after someone owns the condition.
5. If a stream has traffic but no channel name, the operator imports it as HLS or DASH, live or VOD.

### 6.2 Place a viewer, then protect the URL

1. Middleware asks the steering API with the tenant, and with the viewer’s IP, region, network, or subscriber prefix.
2. The matching rule with the lowest priority number names the edge.
3. If the tenant requires signed URLs, middleware asks for a token for that path and client IP. An address outside the allow list is refused. An empty allow list means the tenant has not restricted addresses.

### 6.3 Before the peak

1. The operator opens Capacity and reads current, peak, and projected megabits against the link.
2. A row marked “Add capacity” is a location to grow, not a silent alarm.
3. On Cache, the operator pre-positions a popular channel onto a chosen edge, or purges a file, channel, group, or a whole cache. The request is audited.

### 6.4 Support case

1. Support staff for one franchisee open Sessions and see only that tenant’s viewing sessions.
2. If the account has `subscriber_data:read`, they resolve one session and see the subscriber code and the attributes that tenant’s profile allows.
3. The resolution is written to the audit log. A NOC operator without that permission is refused. Support for a different tenant is told the session does not exist.

### 6.5 Close usage

1. Nodes report cumulative counters. The service stores the increase in body bytes on 5-minute buckets.
2. After a bucket is old enough, and the hour it belongs to is closed, the hour becomes a finalised usage period.
3. A gap in batch sequence, or a counter that cannot be trusted, marks the period so finance does not treat it as clean.
4. If the figure must change, billing or NOC adds an adjustment with a reason. The original period stays as it was.
5. A webhook can notify an external system that the period is finalised.

## 7. Functional requirements

Priority is MoSCoW. **Must** items are the operating contract. **Should** items are the console workflows operators rely on. **Could** items improve operations without changing the contract.

### 7.1 Access and tenancy

| ID | Priority | Requirement |
| --- | --- | --- |
| BR-ACC-01 | Must | A person signs in with a username and password. An unknown or inactive account is rejected and is not told which part failed. |
| BR-ACC-02 | Must | A signed-in session lasts 8 hours, can be ended explicitly, and is protected against cross-site form posts on state-changing console actions. |
| BR-ACC-03 | Must | Roles are super administrator, NOC, tenant user, support, and billing. A tenant user, support user, or billing user is bound to one tenant. |
| BR-ACC-04 | Must | Tenant-bound users see sessions, programmes, steering, capacity, and usage only for their tenant. A session outside that tenant is not found, rather than described as forbidden. |
| BR-ACC-05 | Must | Billing users do not see sessions, programmes, syslog, steering, cache, or capacity. |
| BR-ACC-06 | Must | Only a super administrator manages tenants, users, and the tenant access policy, and only a super administrator can display a node signing secret. |
| BR-ACC-07 | Must | The audit log is visible to the super administrator and NOC. It records login, channel import, subscriber resolution, usage finalisation, usage adjustment, user creation, salt rotation, webhook creation, origin pairing, steering changes, cache purge, cache pre-position, and access-policy changes. |
| BR-ACC-08 | Should | The console navigation shows only the areas the role may use. |
| BR-ACC-09 | Could | Login sessions are kept in Redis when Redis is reachable, and in the application database otherwise, with no change in business behaviour. |
| BR-ACC-10 | Must | A super administrator can require signed playback tokens for a tenant and can set an IP allow list of addresses or CIDR ranges. An empty allow list does not restrict addresses. |

### 7.2 Node estate

| ID | Priority | Requirement |
| --- | --- | --- |
| BR-NODE-01 | Must | A node is edge, mid, or origin. It has a name, region, city, link capacity, base URL, priority, tenant (edges that carry a franchisee), enablement, and a signing secret. Edges may sit in the operator network or a partner ISP network. |
| BR-NODE-02 | Must | A disabled node cannot submit telemetry. |
| BR-NODE-03 | Must | A node is online after accepted telemetry and offline when it has not been seen for 45 seconds. An offline edge or mid-tier node opens an alert. An offline origin opens an origin-failure alert. |
| BR-NODE-04 | Should | An authorised operator can register a node and enable or disable it from the console. |
| BR-NODE-05 | Should | The node page shows recent health: CPU, memory, cache fill, connections, nginx state, and traffic deltas. |

### 7.3 Origins

| ID | Priority | Requirement |
| --- | --- | --- |
| BR-ORG-01 | Must | NOC can pair two distinct origin servers as primary and secondary. The pairing is audited. |
| BR-ORG-02 | Must | An origin is available when it is enabled and not offline. Otherwise it is unavailable. |
| BR-ORG-03 | Must | The active origin is the primary while the primary is available. If the primary is unavailable and the secondary is available, the active origin is the secondary and the result is marked redirected. If neither is available, there is no active origin. |
| BR-ORG-04 | Must | When an origin misses telemetry, the alert says which secondary to use when one is still available, or that no secondary is available. The alert closes when telemetry resumes. |
| BR-ORG-05 | Must | The origin map lists each pair, the health of both origins, the active origin, and whether traffic was redirected. Callers may cache it for 30 seconds. |

### 7.4 Operations console

| ID | Priority | Requirement |
| --- | --- | --- |
| BR-NOC-01 | Must | For the last 15 minutes of samples, the overview shows cache hit ratio (%), bandwidth saved (%), total CDN traffic, origin traffic, peak rate in Mbps or Gbps, concurrent streams, average response time, edge node availability (%), origin availability (%), and CPU, memory, and storage. |
| BR-NOC-02 | Must | Edge availability is the share of enabled edges that are online and reporting the HTTP service as running. Origin availability is the share of enabled origins that are online. |
| BR-NOC-03 | Must | Cache hit ratio is hit bytes divided by hit plus miss bytes. Bandwidth saved percent is delivered body bytes minus upstream bytes, divided by delivered body bytes. |
| BR-NOC-04 | Must | Peak rate is the highest edge transmit rate in the window. It is shown in Gbps when it is at least 1,000 Mbps, and in Mbps otherwise. |
| BR-NOC-05 | Must | The overview shows traffic by region for edges, and the top 10 channels over the last 6 hours. |
| BR-NOC-06 | Should | The overview lists the latest open alerts and refreshes while the operator is on the page. The 95th-percentile response time remains available for operations even though it is not one of the quoted tiles. |
| BR-NOC-07 | Must | Alerts can be acknowledged by an operator other than billing. Acknowledgement records who and when. |
| BR-NOC-08 | Must | The service opens alerts for cache volume at or above 90 percent full (major below 95 percent, critical at or above), inode pressure, service bandwidth at or above 80 percent of interface speed, traffic at or above link capacity, error or abort rates that mean service degradation, an edge or mid-tier node going offline, and an origin going offline. A cache-full or bandwidth alert closes when the condition clears. |
| BR-NOC-09 | Should | Reports repeat the overview measures and rank channels for the signed-in scope. |

Service degradation means at least 2 percent of requests in the sample are server errors, or at least 5 percent are client aborts. Abnormal traffic means transmit rate is at or above the interface speed. High bandwidth means transmit rate is at or above 80 percent of that speed.

### 7.5 Channels

| ID | Priority | Requirement |
| --- | --- | --- |
| BR-CH-01 | Must | A channel is a stream UUID plus a name. It has a delivery protocol of HLS, DASH, or both, and a kind of live TV or VOD. It may have a code, category, and logo. |
| BR-CH-02 | Must | Import accepts JSON or CSV from a tenant API key, and reports how many rows were created, updated, or rejected. |
| BR-CH-03 | Must | A retired stream UUID cannot be reassigned to a different name or code. The same UUID may be imported again only if it is not being given a new identity. |
| BR-CH-04 | Should | An operator can add a channel, choose protocol and kind, and retire it from the console. Idle channels (never seen, not retired) are visible so the catalogue can be cleaned up. |
| BR-CH-05 | Must | Traffic for a stream UUID that has no channel is kept and labelled unmapped. It is not dropped. |

### 7.6 Steering

| ID | Priority | Requirement |
| --- | --- | --- |
| BR-STR-01 | Must | A steering rule belongs to a tenant and sends matching viewers to one edge. The match is an IP address or CIDR, a region, a network ASN, or a subscriber-code prefix. |
| BR-STR-02 | Must | When more than one rule matches, the lowest priority number wins. |
| BR-STR-03 | Must | NOC can add a rule from the console. The change is audited. A franchisee user sees only that franchisee’s rules. |
| BR-STR-04 | Must | The steering API, given a tenant code and a viewer attribute, returns the chosen edge’s name, base URL, region, and city. An unknown tenant, or no match, is not found. |

### 7.7 Cache fill and purge

| ID | Priority | Requirement |
| --- | --- | --- |
| BR-CACHE-01 | Must | NOC can request that a channel be pre-positioned on a selected enabled edge, so popular content is cached close to subscribers before a peak. |
| BR-CACHE-02 | Must | NOC can request a purge of a file, a channel, a content group, or a complete cache. A complete-cache purge may name one edge or all edges. A narrower purge must name its target. |
| BR-CACHE-03 | Must | Each pre-position and each purge records who asked, when, and the target. The same facts are written to the audit log. |
| BR-CACHE-04 | Must | A CMS or orchestration client can request a purge with the tenant API key. The request is accepted for the node agent to perform. The console does not itself delete bytes on the edge. |
| BR-CACHE-05 | Should | The cache page lists titles that are busy now, so the operator can choose what to pre-position. |

### 7.8 Capacity

| ID | Priority | Requirement |
| --- | --- | --- |
| BR-CAP-01 | Must | For each enabled node in the caller’s scope, the capacity page shows current traffic, the peak over the last 15 minutes, a projection, the link size, and utilisation. |
| BR-CAP-02 | Must | Projected load is that peak plus 15 percent. |
| BR-CAP-03 | Must | A location needs additional capacity when current traffic is at least 70 percent of the link, or the projection is at least 80 percent of the link. The page says so. Other locations are within plan. |

### 7.9 Sessions, privacy, and audience

| ID | Priority | Requirement |
| --- | --- | --- |
| BR-PRIV-01 | Must | Middleware registers a session with the tenant API key. The handle is `1.<pseudonym>.<nonce>`. The pseudonym must match the tenant salt and the subscriber code. |
| BR-PRIV-02 | Must | The tenant code in the body must match the API key. A handle already registered to a different subscriber or tenant is a conflict. Registering the same handle again returns the original stored and dropped attributes. |
| BR-PRIV-03 | Must | Only attributes marked required or optional on that tenant’s profile are stored. Attributes marked not collected or observed, and any attribute the profile does not name, are dropped. The response lists stored and dropped names. |
| BR-PRIV-04 | Must | A required attribute other than the subscriber code, when missing, rejects the registration and names what is missing. |
| BR-PRIV-05 | Must | If the tenant is configured to truncate client IP, a stored client IP is truncated before it is kept. |
| BR-PRIV-06 | Must | Batch registration accepts 1 to 500 sessions and returns a result per session. |
| BR-PRIV-07 | Must | Resolving a viewing session to a subscriber code requires `subscriber_data:read`. The resolver must be allowed to see that tenant. Every resolution that passes the permission check is audited. |
| BR-PRIV-08 | Must | If the directory entry is gone, or the viewing handle has been redacted, resolution returns unresolved and does not guess an identity. |
| BR-PRIV-09 | Must | Programme audience is the time overlap between a viewing session and the programme on the same stream. If both actual start and actual end are present, the result is labelled as-run. Otherwise it is labelled as-published. |
| BR-PRIV-10 | Must | Audience figures are average audience (overlap divided by programme duration), reach at 60 seconds, reach at 300 seconds, average time viewed among people who reached 60 seconds, and peak audience by minute. |
| BR-PRIV-11 | Should | NOC can enter a programme against a live channel. Tenant users can read audience for programmes in their tenant. |
| BR-PRIV-12 | Must | A tenant may publish its active subscriber base and the as-of time, for reach comparisons. The figure is an estate total supplied by the tenant, not a count invented from sessions. |
| BR-PRIV-13 | Must | A signed playback token is an HMAC of the path, the expiry, and the client IP, using the tenant salt. It expires four hours after issue. If signed URLs are not required for that tenant, the API says the path is unsigned. |

### 7.10 Usage and billing

| ID | Priority | Requirement |
| --- | --- | --- |
| BR-BILL-01 | Must | Billable usage is body bytes, summed into 5-minute buckets per node, attributed to the node’s tenant. |
| BR-BILL-02 | Must | A sequence gap flags the bucket. A delta that cannot be trusted places the bucket on quality hold. |
| BR-BILL-03 | Must | An hour is finalised only after its buckets are at least 10 minutes old and the hour itself has closed. The period stores the sum of body bytes and whether any included bucket had a gap or a quality hold. |
| BR-BILL-04 | Must | Finalising the same hour twice does not create a second period. |
| BR-BILL-05 | Must | An adjustment is a new row: who, when, the byte delta, and a reason. It does not edit the finalised period. |
| BR-BILL-06 | Must | NOC and billing users can finalise and adjust. A billing user is limited to their tenant’s periods and buckets on screen. |
| BR-BILL-07 | Must | The tenant usage API returns recent finalised periods and open buckets, including gap and quality-hold flags, and only for the tenant of the API key. |
| BR-BILL-08 | Must | When a period is finalised, active webhooks subscribed to `usage.period_finalised` are notified with the period id, tenant, gap flag, and body bytes. |
| BR-BILL-09 | Must | Syslog is not a billing source. It is a forensic store. |

### 7.11 Interfaces for other systems

| ID | Priority | Requirement |
| --- | --- | --- |
| BR-API-01 | Must | Nodes post signed telemetry and signed syslog. Middleware registers sessions with the tenant API key. |
| BR-API-02 | Must | The edge map returns enabled edge nodes for an active tenant: node id, name, base URL, priority, health, region, and city. Health is unhealthy when the node is offline, degraded when a major or critical alert is open, and healthy otherwise. Sort order is health, then priority, then name. The map declares a time-to-live of 60 seconds. An unknown or inactive tenant is not found. |
| BR-API-03 | Must | The origin map, steering API, signed-token API, channel import, subscriber-base update, usage read, and cache purge are available to the systems named in section 4. |
| BR-API-04 | Should | A super administrator can register a webhook. Supported business events are `alert.opened` and `usage.period_finalised`. Each delivery is recorded with status or error. |

### 7.12 Telemetry ingest

| ID | Priority | Requirement |
| --- | --- | --- |
| BR-ING-01 | Must | The service checks the HMAC of the raw body before it decompresses or interprets the batch. A bad signature is rejected. |
| BR-ING-02 | Must | The signed timestamp must be within 300 seconds of the server clock. |
| BR-ING-03 | Must | The node id and batch id in the body must match the headers and the signing node. A mismatch is a critical alert and is rejected. |
| BR-ING-04 | Must | Schema major version 2 is accepted. Any other major version is rejected as unimplemented. |
| BR-ING-05 | Must | The declared node tier must match the registered tier. A sample group that does not belong to that tier is rejected. |
| BR-ING-06 | Must | Pre-computed rates are not accepted. The service derives rates from counters. |
| BR-ING-07 | Must | A batch the node has already submitted is a success and is not applied twice. |
| BR-ING-08 | Must | Inside one counter epoch, a sequence that skips ahead is accepted and recorded as a gap. A sequence that goes backwards, and is not marked backfill, is rejected. |
| BR-ING-09 | Must | When the counter epoch changes, the service does not invent a delta from the previous epoch’s counters. A counter that moves backwards inside an epoch is a data-quality event and is not billed as a fresh spike. Usage bytes never go negative. |
| BR-ING-10 | Must | Viewing sessions are accepted only from an edge that belongs to a tenant. A session that contains a subscriber code or other personal field is rejected. |
| BR-ING-11 | Must | A batch contains 1 to 240 samples, at most 1,000 sessions, and at most 100 events. Sample interval is greater than 0 and at most 900 seconds. |
| BR-ING-12 | Should | The service records content and viewership summaries carried in the sample, and raises a minor alert the first time an unknown stream UUID is seen. |

### 7.13 Retention

| ID | Priority | Requirement |
| --- | --- | --- |
| BR-RET-01 | Must | Session-directory rows older than the tenant’s retention period are deleted. ACME’s default is 90 days. Beta’s default is 30 days. |
| BR-RET-02 | Must | Viewing sessions older than 90 days have the session handle and client IP removed and are marked redacted. Aggregate viewing facts remain. |
| BR-RET-03 | Must | Syslog messages older than 14 days are deleted. |

## 8. Business rules that must not be relaxed

These rules protect revenue, continuity, and privacy. A later design may change screens and storage. It may not change the outcome.

1. Verify the signature before reading the body.
2. Do not bill a rate field. Bill the counter delta, in body bytes.
3. Do not bill across a counter-epoch change.
4. Do not let a backwards counter become a large positive invoice line.
5. A duplicate batch is success, not an error and not a second charge.
6. A retired stream UUID is not recycled.
7. A node never supplies personal data. Middleware does, and only the fields that tenant’s profile allows.
8. Subscriber resolution is permissioned, tenant-scoped, and audited.
9. Syslog never feeds an invoice.
10. An adjustment never overwrites a finalised period.
11. When the primary origin is down and the secondary is up, the origin map names the secondary. It does not keep sending viewers to the failed primary.
12. A signed token is bound to the path, the expiry, and the client IP. An address outside the tenant allow list does not receive one.
13. A purge or a pre-position is a new audited request. It does not rewrite usage.

## 9. Console information the business expects to see

| Screen | Who | Business content |
| --- | --- | --- |
| Overview | All signed-in roles, scoped for a tenant user | The KPI set in BR-NOC-01, traffic by region, top 10 channels, open alerts |
| Nodes | All signed-in roles, scoped where the role is tenant-bound | Inventory, status, enablement |
| Origins | All signed-in roles | Primary, secondary, health, and the origin currently in use |
| Channels | Operators | Catalogue, protocol, live or VOD, idle and retired state |
| Steering | NOC, tenant user, support | Rules that place a viewer on an edge |
| Cache | NOC, tenant user, support | Pre-position and purge requests. Only NOC and the super administrator submit them |
| Capacity | NOC, tenant user, support | Current, peak, projected load, and locations that need capacity |
| Alerts | Operators except billing for acknowledgement | Open and acknowledged conditions |
| Sessions | NOC, tenant user, support | Pseudonymous viewing sessions; resolve only with permission |
| Reports | All signed-in roles | KPI and channel ranking for the caller’s scope |
| Billing | Super administrator, NOC, billing, tenant user, support | Periods, buckets, adjustments |
| Programmes | NOC, tenant user, support | Audience with as-run or as-published confidence |
| Syslog | NOC, tenant user, support | Recent forensic lines, 14-day life |
| Audit | Super administrator, NOC | Sensitive actions, including cache and steering changes |
| Tenants and users | Super administrator | Franchisees, salts, signed-URL policy, IP allow list, accounts |

## 10. Non-functional requirements

| ID | Requirement |
| --- | --- |
| BR-NFR-01 | The console and ingest service run as one application. Local preview uses SQLite so an operator can run it without a database server. Production uses PostgreSQL 16. |
| BR-NFR-02 | Metric and viewing-session data use the same columns in preview and production, so a large estate can move those tables to time-series storage without changing the business contract. |
| BR-NFR-03 | Passwords are stored only as a one-way hash. Tenant API keys are stored only as a hash. Node signing secrets are shown in the console only to a super administrator who asks for them. |
| BR-NFR-04 | Telemetry bodies larger than 2,000,000 bytes compressed, or 8,388,608 bytes after decompression, are rejected. |
| BR-NFR-05 | Webhook delivery does not block the operator. A failed delivery is recorded. It does not roll back the usage period. |
| BR-NFR-06 | The contract rules in section 8 are covered by automated tests and must keep failing closed: bad signature, wrong tier, unknown schema, duplicate batch, epoch reset, negative delta, profile filtering, tenant isolation, retired UUID, 60-second edge-map TTL, origin redirect, and signed-token IP refusal. |
| BR-NFR-07 | Playback tokens and tenant API keys travel only over HTTPS in production. The local preview may use HTTP on the operator’s own machine. |

## 11. Assumptions and dependencies

| ID | Statement |
| --- | --- |
| A-1 | Node agents can sign the raw body and send cumulative counters, a counter epoch, and a sequence. They do not send subscriber codes. |
| A-2 | Middleware can build `1.<pseudonym>.<nonce>` with the tenant salt and can call the register, edge-map, origin-map, steering, and token APIs. |
| A-3 | Each billable edge is assigned to exactly one tenant. Shared mid and origin nodes are not themselves a tenant invoice line; origin traffic is an operational measure. |
| A-4 | Programme “as-run” times, when used, are supplied by the operator or a future guide feed. If they are absent, audience is honestly labelled as-published. |
| A-5 | Finance owns the decision to invoice a period that is flagged with a gap or quality hold. The product only marks it. |
| A-6 | Redis is optional. Its absence does not change who can sign in or what they can see. |
| A-7 | The node agent honours a purge or pre-position request. CDN Manager’s duty is to accept, record, and audit it. |
| A-8 | Partner-ISP edges are registered like any other edge. Logical isolation is by tenant, not by a separate console. |

## 12. Acceptance criteria

The business can accept a release when all of the following are true.

1. An operator signs in, sees the overview KPI set, and a tenant user cannot see another tenant’s sessions, steering, or usage.
2. A badly signed batch is rejected. The same good batch posted twice is accepted once and reported as a duplicate the second time.
3. A counter reset does not add the new small counter on top of the old large one as if it were delivered bytes.
4. ACME can register a session with device and DRM fields. Beta, given the same device and DRM fields, stores none of them and says they were dropped.
5. NOC cannot resolve a subscriber. ACME support can resolve an ACME session and cannot resolve, or even discover, a session outside ACME. The resolution appears in the audit log.
6. A retired channel UUID cannot be imported under a new name. A new channel can be marked HLS or DASH, and live or VOD.
7. The edge map for a tenant lists only that tenant’s enabled edges and tells the caller to cache it for 60 seconds.
8. With the primary origin offline and the secondary online, the origin map returns the secondary and says traffic was redirected.
9. A viewer IP inside an ACME steering range resolves to that edge. A token request from an address outside the allow list is refused. An address inside the list receives a signed token.
10. A purge and a pre-position each appear in the audit log with the actor and the target.
11. A node at or above 70 percent of its link is marked as needing capacity.
12. A finalised hour shows body bytes and a gap flag when a sequence was missing. An adjustment appears as its own line with a reason.
13. Syslog older than 14 days is gone, and no billing screen or usage API reads it.

## 13. Open points for the business review

These are not decided by the current contract. They should be confirmed before this document is treated as final.

| ID | Question | Suggested default if the business does not decide |
| --- | --- | --- |
| Q-1 | Who may approve an adjustment above a given byte threshold? | Any NOC or billing user may adjust, and the audit log is the control. |
| Q-2 | Should a period with a gap or quality hold be blocked from export until a person clears it? | Export it with the flag. Finance decides. |
| Q-3 | Is 90 days the right redaction age for viewing handles for every tenant, or should it follow each tenant’s directory retention? | Keep 90 days for viewing handles. Directory retention stays per tenant. |
| Q-4 | Should tenant users be allowed to finalise usage, or only to read it? | Read for tenant users and support. Finalise and adjust for NOC and billing. |
| Q-5 | The tenant record has a Track B flag that no business rule uses yet. What should Track B mean? | Leave it unused until a separate requirement defines it. |
| Q-6 | Are webhook consumers internal only, and is a shared secret in a header enough? | Yes for this release. |
| Q-7 | Should a purge wait for the node to confirm completion before the console marks it done? | Accept the request as “requested”. Completion status can be added when the agent reports back. |

## 14. Glossary

| Term | Meaning |
| --- | --- |
| Tenant | A franchisee or operator whose subscribers and usage are isolated. Also called an MSO, ISP, or enterprise customer. |
| Edge / mid / origin | Delivery node close to the subscriber, a cache tier in front of origin, and the origin itself. |
| Primary / secondary | The origin that normally serves a group, and the origin that takes over when the primary is unavailable. |
| Redirected | The origin map’s statement that viewers should use the secondary because the primary is down. |
| Counter epoch | The generation of a node’s cumulative counters. A new epoch means the counters started again. Deltas must not cross it. |
| Body bytes | Bytes of response body delivered to clients. This is the billing quantity. |
| Bandwidth saved | Delivered body bytes minus bytes pulled from upstream, expressed as a percent of delivered bytes on the overview. |
| Quality hold | A mark that a bucket’s counters could not be trusted, so the hour should not be treated as a clean invoice line without review. |
| Session handle | Opaque id `1.<pseudonym>.<nonce>` that ties a viewing session to a directory entry without putting the subscriber code on the node. |
| Pseudonym | Short tenant-specific stand-in for a subscriber code. The same code in two tenants does not match. |
| Profile | The list of attributes a tenant is allowed to store, each required, optional, not collected, or observed. |
| Steering rule | A match on IP range, region, network, or subscriber prefix that names the edge to use. |
| Pre-position | An instruction to fill an edge with a title before viewers request it. |
| Purge | An instruction to drop a file, channel, content group, or a whole cache. |
| As-run / as-published | Audience measured against actual transmission times, or against the scheduled guide when actual times are missing. |
| Edge map | The tenant’s enabled edges, with health, for middleware to cache for 60 seconds. |
| Origin map | The active origin for each primary and secondary pair, for middleware to cache for 30 seconds. |
| Signed token | A playback proof bound to a path, an expiry, and a client IP. |

## 15. Document history

| Version | Date | Notes |
| --- | --- | --- |
| 0.1 | 7 October 2026 | Draft taken from the operating contract and the console: ingest, privacy, usage, edge map, and roles. |
| 1.0 | 8 October 2026 | Updated from the high-level business requirements. Adds origin failover, HLS and DASH, live and VOD, steering, cache pre-position and purge, capacity, the NOC KPI set, and signed playback tokens with an IP allow list. |
