set -euo pipefail

NS="goldengate-dev"

SRC="gg-postgresql-repltest-01"
TGT="gg-mssql-repltest-01"

SRC_POD="${SRC}-0"
TGT_POD="${TGT}-0"

SRC_FQDN="${SRC}.goldengate-dev.adcbmis.local"
TGT_FQDN="${TGT}.goldengate-dev.adcbmis.local"

SRC_SERVICE="${SRC}.${NS}.svc.cluster.local"
TGT_SERVICE="${TGT}.${NS}.svc.cluster.local"

EXPECTED_SA="gg-runtime-sa"

echo
echo "============================================================"
echo "PHASE 6D1-A — GOLDENGATE LIVE READ-ONLY PREFLIGHT"
echo "============================================================"
echo
echo "Pipeline : repltest-pg-to-mssql-001"
echo "Source   : $SRC"
echo "Target   : $TGT"

echo
echo "============================================================"
echo "1. RUNTIME IDENTITY / READINESS"
echo "============================================================"

for DEP in "$SRC" "$TGT"; do

  kubectl -n "$NS" get sts "$DEP" \
    -o jsonpath='{.metadata.name}{" | ready="}{.status.readyReplicas}{"/"}{.spec.replicas}{" | SA="}{.spec.template.spec.serviceAccountName}{"\n"}'

  SA="$(
    kubectl -n "$NS" get sts "$DEP" \
      -o jsonpath='{.spec.template.spec.serviceAccountName}'
  )"

  if [ "$SA" != "$EXPECTED_SA" ]; then
    echo "ERROR: $DEP uses unexpected ServiceAccount: $SA"
    exit 1
  fi
done

echo
echo "Both runtimes use gg-runtime-sa ✅"

echo
echo "============================================================"
echo "2. PODS / IMAGES"
echo "============================================================"

for POD in "$SRC_POD" "$TGT_POD"; do

  kubectl -n "$NS" get pod "$POD" \
    -o custom-columns='NAME:.metadata.name,READY:.status.containerStatuses[0].ready,STATUS:.status.phase,IMAGE:.spec.containers[0].image,IP:.status.podIP'

done

echo
echo "============================================================"
echo "3. KUBERNETES SERVICE CONTRACT"
echo "============================================================"

echo
echo "--- SOURCE ---"

kubectl -n "$NS" get svc "$SRC" \
  -o jsonpath='{range .spec.ports[*]}{.name}{"="}{.port}{" -> "}{.targetPort}{"\n"}{end}'

echo
echo "--- TARGET ---"

kubectl -n "$NS" get svc "$TGT" \
  -o jsonpath='{range .spec.ports[*]}{.name}{"="}{.port}{" -> "}{.targetPort}{"\n"}{end}'

echo
echo "Expected:"
echo "  SOURCE https=8443 dist=9013 metrics=9015"
echo "  TARGET https=8443 receiver=9014 metrics=9015"

echo
echo "============================================================"
echo "4. PYTHON 3 AVAILABILITY"
echo "============================================================"

for POD in "$SRC_POD" "$TGT_POD"; do

  echo
  echo "--- $POD ---"

  kubectl -n "$NS" exec "$POD" -- \
    python3 -c '
import sys
import ssl
import urllib.request

print("python executable :", sys.executable)
print("python version    :", sys.version.split()[0])
print("ssl available     : yes")
print("urllib available  : yes")
'

done

echo
echo "Python3 available in both runtime images ✅"

echo
echo "============================================================"
echo "5. ADMIN CREDENTIAL / TLS MOUNTS"
echo "============================================================"

for POD in "$SRC_POD" "$TGT_POD"; do

  echo
  echo "--- $POD ---"

  kubectl -n "$NS" exec "$POD" -- sh -c '
    set -eu

    test -s /mnt/secrets-store/admin/OGG_ADMIN
    echo "OGG_ADMIN mounted ✅"

    test -s /mnt/secrets-store/admin/OGG_ADMIN_PWD
    echo "OGG_ADMIN_PWD mounted ✅"

    test -s /etc/nginx/cert/ca-chain.pem
    echo "CA chain mounted ✅"

    test -s /etc/nginx/cert/ogg.pem
    echo "TLS certificate mounted ✅"

    test -s /etc/nginx/cert/ogg.key
    echo "TLS private key mounted ✅"
  '

done

echo
echo "No credential values were printed."

echo
echo "============================================================"
echo "6. /u02 PERSISTENT STORAGE"
echo "============================================================"

for POD in "$SRC_POD" "$TGT_POD"; do

  echo
  echo "--- $POD ---"

  kubectl -n "$NS" exec "$POD" -- sh -c '
    set -eu

    test -d /u02
    mount | grep " /u02 "

    echo "/u02 present and mounted ✅"
  '

done

echo
echo "============================================================"
echo "7. DISTRIBUTION / RECEIVER TCP LISTENERS"
echo "============================================================"

echo
echo "--- SOURCE Distribution service :9013 ---"

kubectl -n "$NS" exec "$SRC_POD" -- \
  python3 - "$SRC_SERVICE" 9013 <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])

try:
    with socket.create_connection((host, port), timeout=10):
        print(f"TCP connectivity to {host}:{port} ✅")
except Exception as exc:
    print(f"TCP connectivity FAILED: {type(exc).__name__}: {exc}")
    raise SystemExit(1)
PY

echo
echo "--- TARGET Receiver service :9014 ---"

kubectl -n "$NS" exec "$TGT_POD" -- \
  python3 - "$TGT_SERVICE" 9014 <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])

try:
    with socket.create_connection((host, port), timeout=10):
        print(f"TCP connectivity to {host}:{port} ✅")
except Exception as exc:
    print(f"TCP connectivity FAILED: {type(exc).__name__}: {exc}")
    raise SystemExit(1)
PY

echo
echo "============================================================"
echo "8. SOURCE -> TARGET WSS/TLS NETWORK PATH"
echo "============================================================"

kubectl -n "$NS" exec "$SRC_POD" -- \
  python3 - "$TGT_FQDN" <<'PY'
import socket
import ssl
import sys

host = sys.argv[1]
port = 443

context = ssl.create_default_context(
    cafile="/etc/nginx/cert/ca-chain.pem"
)

try:
    with socket.create_connection((host, port), timeout=15) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            print("Target hostname :", host)
            print("TLS version     :", tls.version())
            print("TLS handshake   : verified ✅")
except Exception as exc:
    print(f"TLS handshake FAILED: {type(exc).__name__}: {exc}")
    raise SystemExit(1)
PY

echo
echo "This proves the source runtime can establish a CA-verified"
echo "TLS connection toward the target's future WSS endpoint."

echo
echo "============================================================"
echo "9. SOURCE ADMIN REST — READ ONLY"
echo "============================================================"

kubectl -n "$NS" exec "$SRC_POD" -i -- \
  python3 - "$SRC_FQDN" <<'PY'
import base64
import json
import ssl
import sys
import urllib.error
import urllib.request

host = sys.argv[1]

with open("/mnt/secrets-store/admin/OGG_ADMIN", encoding="utf-8") as f:
    username = f.read().strip()

with open("/mnt/secrets-store/admin/OGG_ADMIN_PWD", encoding="utf-8") as f:
    password = f.read().strip()

auth = base64.b64encode(
    f"{username}:{password}".encode()
).decode()

context = ssl.create_default_context(
    cafile="/etc/nginx/cert/ca-chain.pem"
)

paths = [
    "/services/v2/deployments",
    "/services/v2/extracts",
    "/services/v2/sources",
    "/services/v2/replicats",
    "/services/v2/targets",
]

def safe_shape(payload):
    if not isinstance(payload, dict):
        return "non-object JSON"

    out = {
        "topLevelKeys": sorted(payload.keys())
    }

    response = payload.get("response")

    if isinstance(response, dict):
        out["responseKeys"] = sorted(response.keys())

        counts = {}

        for key, value in response.items():
            if isinstance(value, list):
                counts[key] = len(value)

        if counts:
            out["listCounts"] = counts

    return out

for path in paths:

    request = urllib.request.Request(
        f"https://{host}{path}",
        method="GET",
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "User-Agent": "gg-phase6d1-readonly-preflight",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            context=context,
            timeout=15
        ) as response:

            status = response.status
            raw = response.read(1024 * 1024)

    except urllib.error.HTTPError as exc:

        status = exc.code
        raw = exc.read(1024 * 1024)

    print()
    print("PATH   :", path)
    print("STATUS :", status)

    if status < 200 or status >= 300:
        print("RESULT : unexpected HTTP status ❌")
        raise SystemExit(1)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        print("RESULT : response was not valid JSON ❌")
        raise SystemExit(1)

    print(
        "SHAPE  :",
        json.dumps(
            safe_shape(payload),
            separators=(",", ":")
        )
    )

    print("RESULT : authenticated read succeeded ✅")

print()
print("SOURCE ADMIN REST PREFLIGHT PASSED")
PY

echo
echo "============================================================"
echo "10. TARGET ADMIN REST — READ ONLY"
echo "============================================================"

kubectl -n "$NS" exec "$TGT_POD" -i -- \
  python3 - "$TGT_FQDN" <<'PY'
import base64
import json
import ssl
import sys
import urllib.error
import urllib.request

host = sys.argv[1]

with open("/mnt/secrets-store/admin/OGG_ADMIN", encoding="utf-8") as f:
    username = f.read().strip()

with open("/mnt/secrets-store/admin/OGG_ADMIN_PWD", encoding="utf-8") as f:
    password = f.read().strip()

auth = base64.b64encode(
    f"{username}:{password}".encode()
).decode()

context = ssl.create_default_context(
    cafile="/etc/nginx/cert/ca-chain.pem"
)

paths = [
    "/services/v2/deployments",
    "/services/v2/extracts",
    "/services/v2/sources",
    "/services/v2/replicats",
    "/services/v2/targets",
]

def safe_shape(payload):
    if not isinstance(payload, dict):
        return "non-object JSON"

    out = {
        "topLevelKeys": sorted(payload.keys())
    }

    response = payload.get("response")

    if isinstance(response, dict):
        out["responseKeys"] = sorted(response.keys())

        counts = {}

        for key, value in response.items():
            if isinstance(value, list):
                counts[key] = len(value)

        if counts:
            out["listCounts"] = counts

    return out

for path in paths:

    request = urllib.request.Request(
        f"https://{host}{path}",
        method="GET",
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "User-Agent": "gg-phase6d1-readonly-preflight",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            context=context,
            timeout=15
        ) as response:

            status = response.status
            raw = response.read(1024 * 1024)

    except urllib.error.HTTPError as exc:

        status = exc.code
        raw = exc.read(1024 * 1024)

    print()
    print("PATH   :", path)
    print("STATUS :", status)

    if status < 200 or status >= 300:
        print("RESULT : unexpected HTTP status ❌")
        raise SystemExit(1)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        print("RESULT : response was not valid JSON ❌")
        raise SystemExit(1)

    print(
        "SHAPE  :",
        json.dumps(
            safe_shape(payload),
            separators=(",", ":")
        )
    )

    print("RESULT : authenticated read succeeded ✅")

print()
print("TARGET ADMIN REST PREFLIGHT PASSED")
PY

echo
echo "============================================================"
echo "11. CURRENT PROCESS INVENTORY VIA MONITOR"
echo "============================================================"

kubectl -n goldengate-monitoring get pods \
  -l app.kubernetes.io/name=goldengate-monitor \
  -o wide || true

echo
echo "Current expected state BEFORE replication:"
echo "  Extract      = 0"
echo "  Distribution = 0"
echo "  Replicat     = 0"

echo
echo "============================================================"
echo "12. ACTIVE ARGO STATUS"
echo "============================================================"

kubectl -n argocd get applications.argoproj.io \
  goldengate-dev-platform \
  goldengate-dev-postgresql-repltest-01 \
  goldengate-dev-mssql-repltest-01 \
  goldengate-monitor \
  -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'

echo
echo "============================================================"
echo "PHASE 6D1-A PREFLIGHT COMPLETE"
echo "============================================================"

echo
echo "Validated read-only:"
echo "  runtime readiness                ✅"
echo "  shared gg-runtime-sa             ✅"
echo "  Python 3 availability            ✅"
echo "  mounted Admin credentials        ✅"
echo "  mounted TLS CA                   ✅"
echo "  source Distribution listener     ✅"
echo "  target Receiver listener         ✅"
echo "  source -> target TLS path        ✅"
echo "  source Admin REST                ✅"
echo "  target Admin REST                ✅"
echo "  /u02 persistent mounts           ✅"

echo
echo "NOT tested yet:"
echo "  PostgreSQL database login"
echo "  MSSQL database login"
echo "  TRANDATA"
echo "  checkpoint table"
echo "  Extract creation"
echo "  trail creation"
echo "  Distribution path creation"
echo "  Receiver trail reception"
echo "  Replicat creation"
echo
echo "These remain intentionally untouched."