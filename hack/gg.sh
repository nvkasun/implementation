Do this now

First verify the deletion/finalizer state:

kubectl get ingress gg-poc-dev-alb-resident \
  -n alb-resident \
  -o jsonpath='{.metadata.deletionTimestamp}{"\n"}{.metadata.finalizers}{"\n"}'

Then disable ALB deletion protection on the Ingress:

kubectl annotate ingress gg-poc-dev-alb-resident \
  -n alb-resident \
  alb.ingress.kubernetes.io/load-balancer-attributes='deletion_protection.enabled=false' \
  --overwrite

Check that it changed:

kubectl get ingress gg-poc-dev-alb-resident \
  -n alb-resident \
  -o jsonpath='{.metadata.annotations.alb\.ingress\.kubernetes\.io/load-balancer-attributes}{"\n"}'

Expected:

deletion_protection.enabled=false

Give the AWS Load Balancer Controller roughly 30–60 seconds to reconcile it.

Because you already issued the delete request, it may disappear automatically after the controller disables protection and deletes the ALB.

Check:

kubectl get ingress gg-poc-dev-alb-resident -n alb-resident

If it still exists after a minute, run the delete once more:

kubectl delete ingress gg-poc-dev-alb-resident \
  -n alb-resident \
  --wait=true

Then:

kubectl get ingress -A