import json
import os
import sys
import urllib.request
import urllib.error

url = os.environ.get("AUTO_SYNC_URL", "").strip()
key = os.environ.get("AUTO_SYNC_KEY", "").strip()

if not url or not key:
    print("AUTO_SYNC_CRON_FAILED missing configuration")
    sys.exit(2)

request = urllib.request.Request(
    url,
    data=b"{}",
    method="POST",
    headers={
        "Content-Type": "application/json",
        "X-Auto-Sync-Key": key,
        "User-Agent": "AI-Revenue-Assistant-AutoSync/1.0",
    },
)

try:
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.loads(response.read().decode("utf-8"))
        print(
            "AUTO_SYNC_CRON_OK "
            f"created={payload.get('created', 0)} "
            f"duplicates={payload.get('duplicates', 0)} "
            f"skipped={payload.get('skipped', 0)}"
        )
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", errors="replace")[:500]
    print(f"AUTO_SYNC_CRON_FAILED http={exc.code} body={body}")
    sys.exit(1)
except Exception as exc:
    print(f"AUTO_SYNC_CRON_FAILED type={type(exc).__name__}")
    sys.exit(1)
