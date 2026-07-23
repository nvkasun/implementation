{{- /*
Shared goldengate-observer sidecar container definition, included once from
each of source-statefulset.yaml and target-statefulset.yaml so the
container spec is defined exactly once. Call as:

  {{- include "goldengate.observerContainer" (dict "root" $ "component" "source") | nindent 8 }}

"component" must be "source" or "target".
*/ -}}
{{- define "goldengate.observerContainer" -}}
{{- $root := .root -}}
{{- $component := .component -}}
{{- $observer := $root.Values.monitoring.observer -}}
{{- $compValues := index $root.Values $component -}}
{{- $pipeline := printf "gg-%s-%s" $root.Values.global.deploymentId $component -}}
- name: goldengate-observer
  image: "{{ required (printf "monitoring.observer.image.repository is required when monitoring.observer.enabled=true (%s)" $component) $observer.image.repository }}:{{ required (printf "monitoring.observer.image.tag is required when monitoring.observer.enabled=true (%s)" $component) $observer.image.tag }}"
  imagePullPolicy: {{ $observer.image.pullPolicy | default "IfNotPresent" }}
  env:
    - name: AWS_REGION
      value: {{ $observer.awsRegion | quote }}
    - name: AWS_DEFAULT_REGION
      value: {{ $observer.awsRegion | quote }}
    - name: AWS_STS_REGIONAL_ENDPOINTS
      value: "regional"
    - name: DYNAMODB_TABLE
      value: {{ $observer.dynamodbTable | quote }}
    - name: PIPELINE
      value: {{ $pipeline | quote }}
    - name: DEPLOYMENT_ID
      value: {{ $root.Values.global.deploymentId | quote }}
    - name: COMPONENT
      value: {{ $component | quote }}
    - name: ENGINE
      value: {{ $compValues.engine | quote }}
    - name: POD_NAME
      valueFrom:
        fieldRef:
          fieldPath: metadata.name
    - name: POD_NAMESPACE
      valueFrom:
        fieldRef:
          fieldPath: metadata.namespace
    - name: ADMIN_HOST
      value: {{ $observer.adminHost | quote }}
    - name: ADMIN_PORT
      value: {{ $compValues.service.ports.https | quote }}
    - name: METRICS_HOST
      value: {{ $observer.metricsHost | quote }}
    - name: METRICS_PORT
      value: {{ $compValues.service.ports.metrics | quote }}
    - name: U02_PATH
      value: {{ $observer.u02Path | quote }}
    - name: CHECK_INTERVAL_SECONDS
      value: {{ $observer.checkIntervalSeconds | quote }}
    - name: CONNECT_TIMEOUT_SECONDS
      value: {{ $observer.connectTimeoutSeconds | quote }}
    - name: HEALTH_LISTEN_HOST
      value: "0.0.0.0"
    - name: HEALTH_LISTEN_PORT
      value: {{ $observer.health.port | quote }}
    - name: CLOUDWATCH_NAMESPACE
      value: {{ $observer.cloudWatchNamespace | quote }}
    - name: OBSERVER_VERSION
      value: {{ $observer.image.tag | quote }}
  ports:
    - name: observer-health
      containerPort: {{ $observer.health.port }}
      protocol: TCP
  volumeMounts:
    - name: u02
      mountPath: {{ $observer.u02Path | quote }}
      readOnly: true
  livenessProbe:
    httpGet:
      path: /healthz
      port: {{ $observer.health.port }}
    initialDelaySeconds: {{ $observer.health.initialDelaySeconds }}
    periodSeconds: {{ $observer.health.periodSeconds }}
    timeoutSeconds: {{ $observer.health.timeoutSeconds }}
    failureThreshold: {{ $observer.health.failureThreshold }}
  securityContext:
    {{- toYaml $observer.securityContext | nindent 4 }}
  resources:
    {{- toYaml $observer.resources | nindent 4 }}
{{- end }}
