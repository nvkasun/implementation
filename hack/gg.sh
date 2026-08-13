So right now, only do these 5 commands
kubectl delete ingress gg-monitor -n goldengate-monitoring --wait=true

kubectl delete ingress argocd-server-ingress -n argocd --wait=true

kubectl delete ingress gg-poc-dev-alb-resident -n alb-resident --wait=true

kubectl get ingress -A

and:

kubectl get pv pvc-0c5458bc-a019-4e9e-b734-2f16b6b24c6f \
  -o jsonpath='{.spec.csi.volumeHandle}{"\n"}'

kubectl get pv pvc-5a38a1af-f8dd-4868-aef3-9b83e110fc26 \
  -o jsonpath='{.spec.csi.volumeHandle}{"\n"}'