set +e

echo
echo "============================================================"
echo "1. AWS IDENTITY"
echo "============================================================"

aws sts get-caller-identity \
  --query '{Account:Account,Arn:Arn,UserId:UserId}' \
  --output table 2>&1

echo
echo "============================================================"
echo "2. CURRENT KUBERNETES CONTEXT"
echo "============================================================"

echo "Current context:"
kubectl config current-context 2>&1

echo
echo "All available contexts:"
kubectl config get-contexts 2>&1

echo
echo "Cluster API details:"
kubectl cluster-info 2>&1

echo
echo "Expected EKS cluster:"
aws eks describe-cluster \
  --region eu-west-1 \
  --name gg-poc-dev \
  --query 'cluster.{Name:name,Arn:arn,Status:status,Endpoint:endpoint}' \
  --output table 2>&1

echo
echo "============================================================"
echo "3. ALL NAMESPACES"
echo "============================================================"

kubectl get namespaces \
  -o custom-columns='NAME:.metadata.name,STATUS:.status.phase,AGE:.metadata.creationTimestamp' \
  2>&1

echo
echo "============================================================"
echo "4. ALL ARGO CD APPLICATIONS"
echo "============================================================"

kubectl get applications.argoproj.io \
  -A \
  -o custom-columns='NAMESPACE:.metadata.namespace,NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status,DESTINATION:.spec.destination.namespace' \
  2>&1

echo
echo "============================================================"
echo "5. ALL GOLDENGATE STATEFULSETS"
echo "============================================================"

kubectl get statefulsets \
  -A \
  -o custom-columns='NAMESPACE:.metadata.namespace,NAME:.metadata.name,READY:.status.readyReplicas,REPLICAS:.status.replicas,CONTAINERS:.spec.template.spec.containers[*].name' \
  2>&1

echo
echo "============================================================"
echo "6. ALL GOLDENGATE-RELATED PODS"
echo "============================================================"

kubectl get pods \
  -A \
  -o custom-columns='NAMESPACE:.metadata.namespace,NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,CONTAINERS:.spec.containers[*].name,NODE:.spec.nodeName' \
  2>&1 |
grep -Ei 'goldengate|gg-|ogg-|NAMESPACE' || true

echo
echo "============================================================"
echo "7. EXPECTED NAMESPACE RESOURCE CHECK"
echo "============================================================"

for NS in \
  goldengate-dev \
  goldengate-monitoring \
  gg-dev-payments-ora-to-pg-001 \
  argocd
do
  echo
  echo "----- Namespace: ${NS} -----"

  kubectl get namespace "$NS" \
    -o custom-columns='NAME:.metadata.name,STATUS:.status.phase,CREATED:.metadata.creationTimestamp' \
    2>&1

  kubectl get statefulsets,deployments,pods,services,ingress,pvc \
    -n "$NS" \
    -o wide \
    2>&1
done

echo
echo "============================================================"
echo "8. EXPECTED ARGO CD APPLICATION CHECK"
echo "============================================================"

for APP in \
  goldengate-dev-oracle-payments-01 \
  goldengate-dev-postgresql-payments-01 \
  goldengate-monitor \
  goldengate-payments-ora-to-pg-001
do
  echo
  echo "----- Application: ${APP} -----"

  kubectl get application "$APP" \
    -n argocd \
    -o jsonpath='name={.metadata.name}{"\n"}sync={.status.sync.status}{"\n"}health={.status.health.status}{"\n"}destination={.spec.destination.namespace}{"\n"}revision={.status.sync.revision}{"\n"}finalizers={.metadata.finalizers}{"\n"}' \
    2>&1
done

echo
echo "============================================================"
echo "9. MONITOR CONFIGURATION"
echo "============================================================"

kubectl get deployment gg-monitor \
  -n goldengate-monitoring \
  -o jsonpath='image={.spec.template.spec.containers[0].image}{"\n"}containers={.spec.template.spec.containers[*].name}{"\n"}serviceAccount={.spec.template.spec.serviceAccountName}{"\n"}{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
  2>&1

echo
echo "============================================================"
echo "10. MONITOR API STATUS"
echo "============================================================"

MONITOR_POD="$(
  kubectl get pods \
    -n goldengate-monitoring \
    -l app.kubernetes.io/name=gg-monitor \
    --field-selector=status.phase=Running \
    --sort-by=.metadata.creationTimestamp \
    -o name 2>/dev/null |
  tail -n 1 |
  sed 's#^pod/##'
)"

echo "Selected monitor pod: ${MONITOR_POD:-NOT_FOUND}"

if [ -n "${MONITOR_POD:-}" ]; then
  kubectl exec -i \
    -n goldengate-monitoring \
    "$MONITOR_POD" \
    -- python3 - <<'PY'
import json
import urllib.request

base = "http://127.0.0.1:8080"

for path in ("/healthz", "/readyz", "/api/status", "/api/processes"):
    print("\n###", path)
    try:
        with urllib.request.urlopen(base + path, timeout=10) as response:
            print("HTTP", response.status)
            print(json.dumps(json.load(response), indent=2, sort_keys=True))
    except Exception as exc:
        print("ERROR:", type(exc).__name__, str(exc))
PY
fi

echo
echo "============================================================"
echo "11. MONITOR DIAGNOSTIC LOGS"
echo "============================================================"

kubectl logs \
  -n goldengate-monitoring \
  deployment/gg-monitor \
  --since=60m \
  2>&1 |
grep -Ei \
  'error|exception|traceback|accessdenied|forbidden|dynamodb|lease|config|poll|endpoint|timeout|failed|cloudwatch|state' \
|| true

echo
echo "============================================================"
echo "12. FINAL SAFETY MESSAGE"
echo "============================================================"

echo "Read-only diagnostics completed."
echo "No resource was deleted or modified."
echo "Do not run the IAM deployment workflow yet."
echo "Do not delete any Argo CD Application or namespace."