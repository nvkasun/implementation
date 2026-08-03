cat > check-container-insights.sh <<'SCRIPT'
#!/usr/bin/env bash
set -u

section() {
  echo
  echo "============================================================"
  echo "$1"
  echo "============================================================"
}

section "1. AMAZON-CLOUDWATCH NAMESPACE"

if kubectl get namespace amazon-cloudwatch >/dev/null 2>&1; then
  echo "FOUND: namespace amazon-cloudwatch"
  kubectl get namespace amazon-cloudwatch
else
  echo "ABSENT: namespace amazon-cloudwatch"
fi

section "2. CLOUDWATCH / OTEL PODS"

kubectl get pods -A -o wide 2>/dev/null |
grep -Ei \
  'NAMESPACE|cloudwatch-agent|amazon-cloudwatch|cloudwatch-observability|aws-otel|adot|otel-collector' \
  || echo "No CloudWatch Agent or OTel pods found"

section "3. CLOUDWATCH / OTEL WORKLOADS"

kubectl get daemonsets,deployments,statefulsets -A -o wide 2>/dev/null |
grep -Ei \
  'NAMESPACE|cloudwatch-agent|amazon-cloudwatch|cloudwatch-observability|aws-otel|adot|otel-collector|gg-fluent-bit' \
  || true

section "4. CLOUDWATCH OBSERVABILITY CUSTOM RESOURCES"

echo "--- Matching API resources ---"

kubectl api-resources 2>/dev/null |
grep -Ei \
  'amazoncloudwatchagent|cloudwatchagent|instrumentation|opentelemetry' \
  || echo "No matching CloudWatch/OTel API resources found"

echo
echo "--- AmazonCloudWatchAgent resources ---"

kubectl get amazoncloudwatchagent -A -o wide 2>/dev/null \
  || echo "No AmazonCloudWatchAgent resource or API is available"

section "5. CLOUDWATCH / OTEL CRDS"

kubectl get crd 2>/dev/null |
grep -Ei \
  'amazoncloudwatchagent|cloudwatch|opentelemetry|instrumentation' \
  || echo "No CloudWatch/OTel CRDs found"

section "6. SERVICEACCOUNTS"

kubectl get serviceaccounts -A 2>/dev/null |
grep -Ei \
  'NAMESPACE|cloudwatch-agent|amazon-cloudwatch|observability|otel|adot' \
  || echo "No CloudWatch Agent or OTel ServiceAccount found"

section "7. EXISTING FLUENT BIT OWNERSHIP"

kubectl get daemonset gg-fluent-bit \
  -n goldengate-dev \
  -o custom-columns='NAME:.metadata.name,DESIRED:.status.desiredNumberScheduled,READY:.status.numberReady,IMAGE:.spec.template.spec.containers[0].image'

echo
echo "Container Insights ownership check completed."
SCRIPT

chmod +x check-container-insights.sh
./check-container-insights.sh