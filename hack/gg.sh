Run:

kubectl patch ingress gg-monitor \
  -n goldengate-monitoring \
  --type=merge \
  -p '{"metadata":{"finalizers":[]}}'

Then:

kubectl get ingress -A

Expected:

No resources found