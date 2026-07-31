After deployment, run only these validations:

kubectl rollout status deployment/gg-monitor \
  -n goldengate-monitoring \
  --timeout=5m
kubectl get deployment gg-monitor \
  -n goldengate-monitoring \
  -o jsonpath='image={.spec.template.spec.containers[0].image}{"\n"}{range .spec.template.spec.containers[0].env[?(@.name=="CLOUDWATCH_PUBLISH_ENABLED")]}{.name}={.value}{"\n"}{end}'

Expected:

image=.../goldengate-monitor:mon-<new-tag>
CLOUDWATCH_PUBLISH_ENABLED=false

Then:

kubectl exec \
  -n goldengate-monitoring \
  deployment/gg-monitor \
  -- python3 - <<'PY'
import collector

print("env_parser_true=", collector._parse_strict_bool_env(" true "))
print("env_parser_1=", collector._parse_strict_bool_env("1"))
print("env_parser_yes=", collector._parse_strict_bool_env("yes"))

collector.CLOUDWATCH_PUBLISH_ENABLED = True
print("config_bool_true=", collector.cloudwatch_enabled_for({"metricsEnabled": True}))
print("config_string_true=", collector.cloudwatch_enabled_for({"metricsEnabled": "true"}))
print("config_integer_one=", collector.cloudwatch_enabled_for({"metricsEnabled": 1}))
PY

Expected:

env_parser_true= True
env_parser_1= False
env_parser_yes= False
config_bool_true= True
config_string_true= False
config_integer_one= False

Finally:

kubectl logs \
  -n goldengate-monitoring \
  deployment/gg-monitor \
  --since=10m |
grep -E 'cloudwatch_client_creation_failed|cloudwatch_put_metric_data_failed|tick failed' || true

Expected while CloudWatch is disabled: no output.