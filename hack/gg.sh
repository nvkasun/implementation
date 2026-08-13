Run these first
kubectl get pods -n goldengate-dev

Then:

kubectl get ingress -A

AWS specifically recommends removing load-balancer-backed Ingress/Service resources before destroying EKS; otherwise the ALB can remain orphaned and block VPC deletion.

Then:

kubectl get pvc -n goldengate-dev

and:

kubectl get pv

Look particularly for anything related to:

gg-postgresql-repltest-01-u02
gg-mssql-repltest-01-u02

and StorageClasses similar to:

gg-efs-dev-gg-postgresql-repltest-01
gg-efs-dev-gg-mssql-repltest-01

Finally, if you have AWS CLI access:

aws efs describe-access-points \
  --file-system-id fs-09bb3373f132d01b0 \
  --region eu-west-1 \
  --query 'AccessPoints[].{AccessPointId:AccessPointId,State:LifeCycleState,Path:RootDirectory.Path}' \
  --output table
aws efs describe-access-points \
  --file-system-id fs-03d4beaa58f19be78 \
  --region eu-west-1 \
  --query 'AccessPoints[].{AccessPointId:AccessPointId,State:LifeCycleState,Path:RootDirectory.Path}' \
  --output table

And mount targets:

aws efs describe-mount-targets \
  --file-system-id fs-09bb3373f132d01b0 \
  --region eu-west-1 \
  --output table
aws efs describe-mount-targets \
  --file-system-id fs-03d4beaa58f19be78 \
  --region eu-west-1 \
  --output table