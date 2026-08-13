Run this to confirm the current state:

kubectl get ingress gg-monitor \
  -n goldengate-monitoring \
  -o jsonpath='{.metadata.deletionTimestamp}{"\n"}{.metadata.finalizers}{"\n"}'

Then get every security-group reference the controller has logged:

kubectl logs \
  -n kube-system \
  deployment/aws-load-balancer-controller \
  --since=30m \
  | grep -Eo 'sg-[0-9a-f]+' \
  | sort -u

Also check whether the controller has a configured backend SG:

kubectl get deployment aws-load-balancer-controller \
  -n kube-system \
  -o jsonpath='{.spec.template.spec.containers[0].args}' \
  | tr ' ' '\n' \
  | grep -Ei 'security-group|backend'