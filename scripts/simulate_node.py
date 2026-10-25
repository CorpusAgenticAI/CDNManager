#!/usr/bin/env python3
"""Post one signed telemetry batch to a running CDN Manager.

The batch uses a fresh counter epoch so it cannot be mistaken for a delta
against counters the preview agent is already sending.
"""

import gzip
import json
import sys
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from app.demo import NODES
from app.security import sign_body


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "edge-hyd-01"
    base = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8080"
    spec = next(item for item in NODES if item["name"] == name)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    epoch = uuid4().hex[:16]
    payload = {
        "schema_version": "2.1",
        "batch_id": str(uuid4()),
        "node_id": spec["id"],
        "node_tier": spec["tier"],
        "agent_version": "1.0.3",
        "sent_at": now,
        "sequence": 1,
        "samples": [
            {
                "ts": now,
                "interval_s": 15,
                "counter_epoch": epoch,
                "system": {"cpu_pct": 22, "mem_used_pct": 40, "ntp_synced": True, "ntp_offset_ms": 1},
                "network": [{"name": "bond0", "role": "service", "speed_mbps": spec["nic"], "tx_bytes_total": 1000, "rx_bytes_total": 100}],
                "http": {"requests_total": 10, "body_bytes_sent_total": 5000, "connections_active": 4},
                "service": {"nginx_running": True},
            }
        ],
    }
    if spec["tier"] != "origin":
        payload["samples"][0]["cache"] = {"hit_bytes_total": 4000, "miss_bytes_total": 1000, "cold": False}
        payload["samples"][0]["upstream"] = [{"peer": "origin-a:4444", "bytes_received_total": 1000, "state": "up"}]
        payload["samples"][0]["storage"] = [{"mount": "/var/cache/nginx", "role": "cache", "used_pct": 30}]
    else:
        payload["samples"][0]["origin"] = {"health_endpoint_ok": True, "replication_state": "ok", "newest_segment_age_s": 2}
        payload["samples"][0]["storage"] = [{"mount": "/var/origin", "role": "other", "used_pct": 30}]
    raw = gzip.compress(json.dumps(payload).encode())
    headers = {
        "content-type": "application/json",
        "content-encoding": "gzip",
        "x-node-id": spec["id"],
        "x-batch-id": payload["batch_id"],
        "x-timestamp": now,
        "x-signature": sign_body(spec["secret"], raw),
        "x-agent-version": "1.0.3",
    }
    response = httpx.post(f"{base}/api/v1/telemetry", content=raw, headers=headers, timeout=10)
    print(response.status_code, response.text)


if __name__ == "__main__":
    main()
