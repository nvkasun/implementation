Before touching the SG rule, run:

kubectl get ingress -A

We know only terminating gg-monitor should remain.

Also:

kubectl get svc -A | grep LoadBalancer

and:

kubectl get targetgroupbindings -A