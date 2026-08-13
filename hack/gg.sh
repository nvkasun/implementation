Do this first

Give it another minute or two and run:

kubectl get ingress -A

If it disappears:

No resources found

then ALB Kubernetes cleanup is complete and we move directly to EFS.

You can also watch it:

kubectl get ingress gg-monitor \
  -n goldengate-monitoring \
  -w

When it finally returns/deletes, Ctrl+C.

If it is still there after ~5 minutes

Then don't force-delete the finalizer. Check the AWS Load Balancer Controller.

First:

kubectl get pods -n kube-system \
  | grep aws-load-balancer-controller

Then:

kubectl logs \
  -n kube-system \
  deployment/aws-load-balancer-controller \
  --since=15m \
  | grep -Ei 'gg-poc-dev-alb|gg-monitor|error|accessdenied|delete|reconcile'

Also:

kubectl describe ingress gg-monitor \
  -n goldengate-monitoring