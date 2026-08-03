NS="amazon-cloudwatch"

echo "Cluster-scraper CR:"
kubectl get amazoncloudwatchagent \
  cloudwatch-agent-cluster-scraper \
  -n "$NS" \
  -o json |
jq '{
  name: .metadata.name,
  generation: .metadata.generation,
  mode: .spec.mode,
  hostNetwork: .spec.hostNetwork
}'

echo "Cluster-scraper Deployment:"
kubectl get deployment \
  cloudwatch-agent-cluster-scraper \
  -n "$NS" \
  -o json |
jq '{
  name: .metadata.name,
  generation: .metadata.generation,
  observedGeneration: .status.observedGeneration,
  hostNetwork: .spec.template.spec.hostNetwork,
  replicas: .status.replicas,
  updatedReplicas: .status.updatedReplicas,
  availableReplicas: .status.availableReplicas
}'



------------------------

SELECTOR="$(
  kubectl get deployment cloudwatch-agent-cluster-scraper \
    -n "$NS" \
    -o json |
  jq -r '
    .spec.selector.matchLabels
    | to_entries
    | map("\(.key)=\(.value)")
    | join(",")
  '
)"

kubectl get pods \
  -n "$NS" \
  -l "$SELECTOR" \
  -o json |
jq -r '
  .items[]
  | select(.metadata.deletionTimestamp == null)
  | [
      .metadata.name,
      .spec.nodeName,
      (.spec.hostNetwork // false),
      .status.hostIP,
      .status.podIP,
      .status.phase
    ]
  | @tsv
'

------------------------

NODE="ip-10-238-84-118.eu-west-1.compute.internal"

kubectl get pods -A \
  --field-selector "spec.nodeName=${NODE}" \
  -o json |
jq -r '
  .items[]
  | select(.metadata.deletionTimestamp == null)
  | select((.spec.hostNetwork // false) == true)
  | [
      .metadata.namespace,
      .metadata.name,
      (.metadata.ownerReferences[0].kind // "-"),
      (.metadata.ownerReferences[0].name // "-"),
      (
        .spec.containers
        | map(.name + "=" + .image)
        | join(",")
      )
    ]
  | @tsv
'

---------------------------
sudo ss -ltnp '( sport = :8888 )'