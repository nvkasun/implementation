What we should do next

Before adding MSSQL, we should finish proving the managed-EFS lifecycle. Terraform creation succeeded and the runtime deployed, but we should now verify the actual chain all the way down to /u02.

Verify the Kubernetes storage chain. Run these read-only commands:
kubectl get sts -n goldengate-dev
kubectl get pods -n goldengate-dev -o wide
kubectl get pvc -n goldengate-dev -o wide
kubectl get pv -o wide
kubectl get storageclass

Find the resources belonging to:

gg-postgresql-repltest-01

The expected relationship is:

gg-postgresql-repltest-01 Pod
            ↓
         /u02
            ↓
           PVC
            ↓
            PV
            ↓
     EFS CSI Access Point
            ↓
 NEW dedicated EFS filesystem

Then run:

kubectl describe pvc <REPLTEST-PVC-NAME> -n goldengate-dev

and:

kubectl get pv <PV-NAME> -o yaml

We especially want the CSI section from that PV. It should identify the EFS CSI driver and the real EFS filesystem/access-point identity.

Then check /u02 from the actual new pod:

kubectl exec -n goldengate-dev <GG-POSTGRESQL-REPLTEST-POD> -- df -hT /u02

and:

kubectl exec -n goldengate-dev <GG-POSTGRESQL-REPLTEST-POD> -- mount

Don't change anything yet. Send me those outputs/screenshots and I'll verify the mapping.

Verify the new EFS in AWS. In the EFS console, find the filesystem with:
Name:
dev-gg-postgresql-repltest-01-efs

We should confirm:

State                    Available
Performance mode         General Purpose
Throughput mode          Elastic
Encrypted                Yes
KMS key                  approved ADCB EFS key

ManagedBy
  goldengate-eks-app

GoldenGateDeploymentId
  gg-postgresql-repltest-01

GoldenGateEnvironment
  dev

GoldenGateStorage
  u02

And there should be the three mount targets we saw Terraform create.

Most importantly:

NEW managed PostgreSQL runtime
        ↓
NEW fs-xxxxxxxx

must NOT equal

fs-05cadf3570f23cd39

The latter must remain the historical EFS for the old deployments.

After those read-only checks, perform the persistence test. This is the real proof that our /u02 architecture works.

Create a harmless marker in a dedicated test directory:

kubectl exec -n goldengate-dev <POD> -- \
  sh -c 'mkdir -p /u02/.vdr-persistence-test && date > /u02/.vdr-persistence-test/marker.txt'

Confirm it:

kubectl exec -n goldengate-dev <POD> -- \
  cat /u02/.vdr-persistence-test/marker.txt

Then recreate only the repltest pod:

kubectl delete pod <POD> -n goldengate-dev

Wait for the StatefulSet to recreate it:

kubectl get pods -n goldengate-dev -w

Once the replacement is Running, check:

kubectl exec -n goldengate-dev <NEW-POD> -- \
  cat /u02/.vdr-persistence-test/marker.txt

If the original marker is still there:

Pod A
   ↓ write marker
EFS-A
   ↓
Pod A deleted
   ↓
Pod B created
   ↓
same EFS-A
   ↓
marker survives ✅

Then we can confidently mark managed /u02 persistence as live-proven.