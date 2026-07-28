{{- define "goldengate-platform.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- /*
Standard platform-level labels. Deliberately generic (platform-wide, not
per-runtime): no goldengate.adcb/deployment-id or similar per-runtime
ownership label belongs here -- the shared namespaces and shared
ServiceAccounts are owned by this platform release as a whole, never by an
individual GoldenGate runtime.
*/ -}}
{{- define "goldengate-platform.labels" -}}
app.kubernetes.io/name: goldengate-platform
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: platform
app.kubernetes.io/part-of: goldengate
app.kubernetes.io/managed-by: {{ .Release.Service }}
goldengate.adcb/environment: {{ required "environment is required." .Values.environment | quote }}
{{- end }}
