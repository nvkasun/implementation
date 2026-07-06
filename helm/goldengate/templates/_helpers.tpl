{{- define "goldengate.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "goldengate.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else }}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end }}
{{- end }}

{{- define "goldengate.labels" -}}
app.kubernetes.io/name: {{ include "goldengate.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/part-of: goldengate
goldengate.adcb/environment: {{ .Values.global.environment | quote }}
goldengate.adcb/deployment-id: {{ .Values.global.deploymentId | quote }}
{{- end }}

{{- define "goldengate.sourceName" -}}
{{- if .Values.source.fullnameOverride }}
{{- .Values.source.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if .Values.source.name }}
{{- .Values.source.name | trunc 63 | trimSuffix "-" -}}
{{- else }}
{{- printf "%s-source" (include "goldengate.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end }}
{{- end }}

{{- define "goldengate.sourceHeadlessName" -}}
{{- printf "%s-headless" (include "goldengate.sourceName" .) | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "goldengate.sourceServiceAccountName" -}}
{{- if .Values.source.serviceAccount.create }}
{{- default (printf "%s-sa" (include "goldengate.sourceName" .)) .Values.source.serviceAccount.name | trunc 63 | trimSuffix "-" -}}
{{- else }}
{{- default "default" .Values.source.serviceAccount.name -}}
{{- end }}
{{- end }}

{{- define "goldengate.sourceSelectorLabels" -}}
app.kubernetes.io/name: {{ include "goldengate.sourceName" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: source
{{- end }}