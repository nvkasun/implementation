aws eks describe-addon \
  --cluster-name gg-poc-dev \
  --addon-name amazon-cloudwatch-observability \
  --region eu-west-1

kubectl get daemonsets -A |
grep -Ei 'cloudwatch-agent|fluent-bit|aws-for-fluent-bit'