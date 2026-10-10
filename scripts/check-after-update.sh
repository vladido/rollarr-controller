#!/bin/sh
# Read-only acceptance check; never queues runs or restarts dependencies.
set -eu
docker exec -i "${1:-rollarr-controller}" python - <<'PY'
import json
import time
import urllib.request

base = "http://127.0.0.1:8080"
def get(path):
    with urllib.request.urlopen(base + path, timeout=5) as response:
        return json.load(response)

# Fail immediately if an older image lacks the dashboard or liveness API.
get("/live")
status = get("/v1/status")
assert status.get("service") == "rollarr", "wrong status API"
assert isinstance(get("/v1/audit?limit=1").get("records"), list), "invalid audit API"
deadline = time.monotonic() + 900
while True:
    status = get("/v1/status")
    run = status.get("last_run") or {}
    if run.get("finished_at") and not status.get("running"):
        assert status.get("last_error") is None, "last cycle failed: " + str(status.get("last_error"))
        assert status.get("last_success_at"), "no completed successful cycle"
        assert get("/v1/health").get("ok") is True, "cycle health failed"
        print(json.dumps({"check": "passed", "version": status.get("version"),
                          "dry_run": status.get("dry_run"),
                          "last_success_at": status["last_success_at"],
                          "summary": run.get("summary")}))
        break
    if time.monotonic() >= deadline:
        raise SystemExit("FAIL: no completed cycle within 900 seconds")
    time.sleep(5)
PY
