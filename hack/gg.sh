set -euo pipefail

NS="goldengate-dev"

SRC="gg-postgresql-repltest-01"
TGT="gg-mssql-repltest-01"

SRC_POD="${SRC}-0"
TGT_POD="${TGT}-0"

SRC_SERVICE="${SRC}.${NS}.svc.cluster.local"
TGT_SERVICE="${TGT}.${NS}.svc.cluster.local"

TGT_FQDN="${TGT}.goldengate-dev.adcbmis.local"

echo
echo "============================================================"
echo "PHASE 6D1-A — CORRECTED NETWORK VALIDATION"
echo "============================================================"

echo
echo "============================================================"
echo "1. SOURCE DISTRIBUTION TCP :9013"
echo "============================================================"

kubectl -n "$NS" exec -i "$SRC_POD" -- \
python3 - "$SRC_SERVICE" 9013 <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])

print("Host :", host)
print("Port :", port)

try:
    sock = socket.create_connection((host, port), timeout=10)
    sock.close()

    print("TCP connection established ✅")

except Exception as exc:

    print(
        "TCP connection FAILED:",
        type(exc).__name__,
        str(exc)
    )

    raise SystemExit(1)
PY


echo
echo "============================================================"
echo "2. TARGET RECEIVER TCP :9014"
echo "============================================================"

kubectl -n "$NS" exec -i "$TGT_POD" -- \
python3 - "$TGT_SERVICE" 9014 <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])

print("Host :", host)
print("Port :", port)

try:
    sock = socket.create_connection((host, port), timeout=10)
    sock.close()

    print("TCP connection established ✅")

except Exception as exc:

    print(
        "TCP connection FAILED:",
        type(exc).__name__,
        str(exc)
    )

    raise SystemExit(1)
PY


echo
echo "============================================================"
echo "3. SOURCE -> TARGET TLS PATH"
echo "============================================================"

kubectl -n "$NS" exec -i "$SRC_POD" -- \
python3 - "$TGT_FQDN" <<'PY'
import socket
import ssl
import sys

host = sys.argv[1]
port = 443

print("Host :", host)
print("Port :", port)

context = ssl.create_default_context(
    cafile="/etc/nginx/cert/ca-chain.pem"
)

try:

    raw = socket.create_connection(
        (host, port),
        timeout=15
    )

    tls = context.wrap_socket(
        raw,
        server_hostname=host
    )

    print("TLS version :", tls.version())
    print("TLS cipher  :", tls.cipher()[0])
    print("CA verified : yes ✅")

    tls.close()

except Exception as exc:

    print(
        "TLS validation FAILED:",
        type(exc).__name__,
        str(exc)
    )

    raise SystemExit(1)
PY


echo
echo "============================================================"
echo "4. MONITOR — DISCOVER REAL POD / LABELS"
echo "============================================================"

kubectl -n goldengate-monitoring get pods \
  -o wide \
  --show-labels

echo
echo "Deployments:"
kubectl -n goldengate-monitoring get deployment -o wide || true

echo
echo "============================================================"
echo "5. SANITIZED GOLDENGATE DEPLOYMENT INVENTORY"
echo "============================================================"

for POD in "$SRC_POD" "$TGT_POD"; do

    echo
    echo "--- $POD ---"

    kubectl -n "$NS" exec -i "$POD" -- python3 - <<'PY'
import base64
import json
import ssl
import urllib.request

with open(
    "/mnt/secrets-store/admin/OGG_ADMIN",
    encoding="utf-8"
) as f:
    username = f.read().strip()

with open(
    "/mnt/secrets-store/admin/OGG_ADMIN_PWD",
    encoding="utf-8"
) as f:
    password = f.read().strip()

auth = base64.b64encode(
    ("%s:%s" % (username, password)).encode()
).decode()

context = ssl.create_default_context(
    cafile="/etc/nginx/cert/ca-chain.pem"
)

request = urllib.request.Request(
    "https://127.0.0.1:8443/services/v2/deployments",
    headers={
        "Authorization": "Basic %s" % auth,
        "Accept": "application/json"
    }
)

# Local Admin endpoint certificate is issued for the deployment
# hostname, therefore disable hostname validation only for this
# local inventory query while retaining CA validation.
context.check_hostname = False

with urllib.request.urlopen(
    request,
    context=context,
    timeout=15
) as response:

    payload = json.loads(
        response.read().decode("utf-8")
    )

items = (
    payload
    .get("response", {})
    .get("items", [])
)

print("deployment item count :", len(items))

for index, item in enumerate(items):

    print()
    print("Item :", index + 1)

    if isinstance(item, dict):

        print(
            "keys :",
            ",".join(sorted(item.keys()))
        )

        for key in (
            "name",
            "deploymentName",
            "status",
            "type"
        ):
            value = item.get(key)

            if isinstance(value, (str, int, bool)):
                print("%s : %s" % (key, value))
PY

done


echo
echo "============================================================"
echo "CORRECTED READ-ONLY VALIDATION COMPLETE"
echo "============================================================"