First: fix the stuck ALB SG ourselves

Open AWS Console → EC2 → Security Groups in eu-west-1, and search these three IDs one at a time:

sg-008230e74585a543c
sg-077d0e9e30e5a6cde
sg-0c7e8e3a14efdf3e3

sg-077d0e9e30e5a6cde may already say not found, because your controller log explicitly showed it successfully deleted.

For the remaining two, inspect Name, Description and Tags. We are looking for tags such as:

elbv2.k8s.aws/cluster = gg-poc-dev

elbv2.k8s.aws/resource = backend-sg

or something associated with:

ingress.k8s.aws/stack = gg-poc-dev-alb

The SG tagged:

elbv2.k8s.aws/resource = backend-sg

is the shared LBC backend SG. Don't delete that one yet just because this particular Ingress is disappearing; LBC uses one shared backend SG across load balancers.

The other controller-created SG associated specifically with gg-poc-dev-alb is the one we're interested in.

Then find why AWS won't delete it

AWS will reject deletion of an SG if it is still:

attached to an ENI
OR
referenced by another security group's rule

which matches the controller's repeated timeout very closely.

In AWS Console go to:

EC2 → Network Interfaces

Use the search/filter:

Security group ID = <candidate-SG-ID>

For example:

Security group ID = sg-008230e74585a543c

and then repeat for:

sg-0c7e8e3a14efdf3e3

If one shows an ENI, inspect its:

Description
Interface type
Status
VPC
Subnet
Security groups

Do not detach/delete an EKS node ENI manually.

If there are no ENIs, the likely dependency is another SG rule referencing it.

In EC2 → Security Groups, inspect the EKS/node/security groups and look for an inbound/outbound rule whose source/destination is the candidate SG:

Source:
sg-008230e74585a543c

or

sg-0c7e8e3a14efdf3e3

If it's clearly an old rule created only for the now-deleted gg-poc-dev-alb, delete that rule only, not the node/cluster SG itself.

Once the dependency is gone, try:

EC2 → Security Groups → candidate controller SG → Actions → Delete security group

AWS will only allow it once there are no ENI or SG-reference dependencies.

Then wait ~30–60 seconds and run:

kubectl get ingress -A

The controller should notice its AWS cleanup is complete, remove:

group.ingress.k8s.aws/gg-poc-dev-alb

and gg-monitor should finally disappear.

Important distinction with your Terraform repo

Your screenshot's:

module "ogg_security_group_efs" {
    ...
    description = "Security group for EFS filesystem - NFS port 2049 from EKS nodes only"
}

is not what we should delete to solve this Ingress problem.