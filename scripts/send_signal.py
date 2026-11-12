"""Simulate an alarm receiver posting a signed Contact ID signal to a running server.

python scripts/send_signal.py --account 1234 --event 130 --zone 003
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime

import httpx

from arcvoice import contact_id
from arcvoice.api import sign


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--account", default="1234")
    p.add_argument("--event", default="130")
    p.add_argument("--zone", default="003")
    p.add_argument("--secret", default=os.environ.get("SIGNAL_WEBHOOK_SECRET", "dev-signal-secret"))
    args = p.parse_args()

    raw = contact_id.build(args.account, args.event, zone=args.zone)
    body = json.dumps({"receiver": "sim-1", "raw": raw, "received_at": datetime.now(UTC).isoformat()}).encode()
    r = httpx.post(f"{args.url}/v1/signals", content=body, headers={"X-Signature": sign(body, args.secret)})
    print(r.status_code, json.dumps(r.json(), indent=2))


if __name__ == "__main__":
    main()
