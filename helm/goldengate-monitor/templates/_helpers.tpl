{{- define "goldengate-monitor.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "goldengate-monitor.labels" -}}
app.kubernetes.io/name: gg-monitor
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/part-of: goldengate-monitor
goldengate.adcb/environment: {{ .Values.global.environment | quote }}
{{- end }}

{{- define "goldengate-monitor.selectorLabels" -}}
app.kubernetes.io/name: gg-monitor
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "goldengate-monitor.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default "gg-monitor" .Values.serviceAccount.name -}}
{{- else }}
{{- default "default" .Values.serviceAccount.name -}}
{{- end }}
{{- end }}

{{- define "goldengate-monitor.secretProviderClassName" -}}
gg-monitor-secrets
{{- end }}
