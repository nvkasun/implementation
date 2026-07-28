{{- define "gg-monitor.labels" -}}
app.kubernetes.io/name: gg-monitor
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: shared-monitor
app.kubernetes.io/part-of: goldengate
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "gg-monitor.selectorLabels" -}}
app.kubernetes.io/name: gg-monitor
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "gg-monitor.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{ required "serviceAccount.name is required when serviceAccount.create=true." .Values.serviceAccount.name }}
{{- else -}}
{{ .Values.serviceAccount.name | default "default" }}
{{- end -}}
{{- end }}
