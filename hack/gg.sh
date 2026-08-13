For now, finish the monitor cleanly

First find the Argo Application owning it:

kubectl get ingress gg-monitor \
  -n goldengate-monitoring \
  -o jsonpath='{.metadata.labels.argocd\.argoproj\.io/instance}{"\n"}'

If that returns an application name, check it:

kubectl get application <APP_NAME> -n argocd

Then, since this entire old cluster is being retired, delete that monitor Argo Application:

kubectl delete application <APP_NAME> \
  -n argocd \
  --wait=true

That is better than manually deleting gg-monitor again because Argo cascade deletion removes the monitor resources and stops self-healing them back.

Afterward:

kubectl get ingress -A

We want zero old GoldenGate ALB Ingresses.

If the first jsonpath command returns nothing, send me:

kubectl get ingress gg-monitor -n goldengate-monitoring -o yaml

or at least:

kubectl get applications -n argocd | grep -i monitor