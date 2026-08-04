Verify whether the actual fix succeeded

Run this:

NS="amazon-cloudwatch"

kubectl get deployment cloudwatch-agent-cluster-scraper \
  -n "$NS" \
  -o json |
jq '{
  uid: .metadata.uid,
  deleting: .metadata.deletionTimestamp,
  generation: .metadata.generation,
  observedGeneration: .status.observedGeneration,
  hostNetwork: .spec.template.spec.hostNetwork,
  replicas: .status.replicas,
  updatedReplicas: .status.updatedReplicas,
  availableReplicas: .status.availableReplicas,
  unavailableReplicas: (.status.unavailableReplicas // 0)
}'

Expected:

hostNetwork: false
generation == observedGeneration
replicas: 1
updatedReplicas: 1
availableReplicas: 1
unavailableReplicas: 0

Then verify the scraper pod:

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
      .status.phase,
      (
        [.status.conditions[]?
          | select(.type == "Ready")
          | .status
        ][0] // "Unknown"
      )
    ]
  | @tsv
'

Expected:

hostNetwork=false
podIP different from hostIP
phase=Running
Ready=True

Finally, verify the node agents:

kubectl get daemonset cloudwatch-agent \
  -n "$NS" \
  -o json |
jq '{
  hostNetwork: .spec.template.spec.hostNetwork,
  desired: .status.desiredNumberScheduled,
  current: .status.currentNumberScheduled,
  updated: .status.updatedNumberScheduled,
  ready: .status.numberReady,
  available: .status.numberAvailable,
  unavailable: (.status.numberUnavailable // 0)
}'

Expected:

hostNetwork: true
desired: 2
ready: 2
available: 2
unavailable: 0

If these outputs match, the live CloudWatch correction succeeded. Only the GitHub workflow logic needs adjustment.

Correct workflow change

Remove this hard requirement:

The Deployment name must become NotFound before recreation is accepted

Replace it with:

1. Record old Deployment UID
2. Delete the Deployment once
3. Poll the Deployment by name
4. NotFound is acceptable but not required
5. Continue while the returned UID equals the old UID
6. Recreation succeeds when the returned UID differs from the old UID
7. Validate the new Deployment has hostNetwork=false

The polling states should be:

No Deployment found
→ deletion completed; continue waiting for recreation

Deployment found with old UID and deletionTimestamp
→ old object is terminating; continue waiting

Deployment found with old UID
→ continue waiting until timeout

Deployment found with different UID
→ new object successfully created; validate it