First identify the EXACT SG the controller is trying to delete

Before we manually change any rule, run this:

kubectl logs \
  -n kube-system \
  deployment/aws-load-balancer-controller \
  --since=60m \
  | grep -B3 -A3 -E '"deleting securityGroup"|"failed to delete securityGroup"'

We are looking for a sequence like:

"deleting securityGroup","securityGroupID":"sg-xxxxxxxx"
...
"failed to delete securityGroup: timed out waiting for the condition"

That securityGroupID is the one actually blocking the finalizer.

If it says:
sg-0c7e8e3a14efdf3e3

then we know the shared backend SG itself is stuck because something still references it.

If it gives another SG

Then we inspect that SG instead. Don't delete sg-0c7... just because it looks controller-owned.

We can already inspect the most likely reference

Open this SG:

sg-008230e74585a543c
Name: gg-poc-dev-node

Make sure you click the node SG, not sg-091549... cluster SG like last time.

Then:

Inbound rules → look at Source

Find any row where Source equals:

sg-0c7e8e3a14efdf3e3