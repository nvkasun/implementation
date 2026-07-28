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

{{- define "goldengate.targetName" -}}
{{- if .Values.target.fullnameOverride }}
{{- .Values.target.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if .Values.target.name }}
{{- .Values.target.name | trunc 63 | trimSuffix "-" -}}
{{- else }}
{{- printf "%s-target" (include "goldengate.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end }}
{{- end }}

{{- define "goldengate.targetHeadlessName" -}}
{{- printf "%s-headless" (include "goldengate.targetName" .) | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "goldengate.targetServiceAccountName" -}}
{{- if .Values.target.serviceAccount.create }}
{{- default (printf "%s-sa" (include "goldengate.targetName" .)) .Values.target.serviceAccount.name | trunc 63 | trimSuffix "-" -}}
{{- else }}
{{- default "default" .Values.target.serviceAccount.name -}}
{{- end }}
{{- end }}

{{- define "goldengate.targetSelectorLabels" -}}
app.kubernetes.io/name: {{ include "goldengate.targetName" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: target
{{- end }}

{{- define "goldengate.efsStorageClassName" -}}
{{- if .Values.persistence.efs.storageClass.name }}
{{- .Values.persistence.efs.storageClass.name | trunc 63 | trimSuffix "-" -}}
{{- else }}
{{- printf "gg-efs-%s-%s" .Values.global.environment .Values.global.deploymentId | trunc 63 | trimSuffix "-" -}}
{{- end }}
{{- end }}

{{- /*
EFS enforces a 100-character path limit on the access point root path,
which EFS CSI builds from storageClass.basePath + "/" + PVC name +
"-" + a unique suffix (ensureUniqueDirectory). basePath and the PVC name
are the only two segments the chart controls, so both default to short
forms: the deployment is still uniquely identified by basePath (scoped
per deploymentId) and, for the PVC name, by the namespace the PVC lives
in -- PVC names are namespaced, so "src-u02"/"tgt-u02" is safe and does
not collide across deployments/namespaces.
*/ -}}

{{- define "goldengate.efsBasePath" -}}
{{- if .Values.persistence.efs.storageClass.basePath }}
{{- .Values.persistence.efs.storageClass.basePath -}}
{{- else }}
{{- printf "/%s" .Values.global.deploymentId -}}
{{- end }}
{{- end }}

{{- define "goldengate.sourceU02PVCName" -}}
{{- if .Values.source.storage.u02.claimName }}
{{- .Values.source.storage.u02.claimName | trunc 63 | trimSuffix "-" -}}
{{- else }}
{{- "src-u02" -}}
{{- end }}
{{- end }}

{{- define "goldengate.targetU02PVCName" -}}
{{- if .Values.target.storage.u02.claimName }}
{{- .Values.target.storage.u02.claimName | trunc 63 | trimSuffix "-" -}}
{{- else }}
{{- "tgt-u02" -}}
{{- end }}
{{- end }}

{{- /*
Phase 1 single-runtime helpers (deploymentModel: singleRuntime). Kept
entirely separate from the source/target helpers above -- legacyPair
rendering never calls any of these.
*/ -}}

{{- define "goldengate.deploymentModel" -}}
{{- default "legacyPair" .Values.deploymentModel -}}
{{- end }}

{{- define "goldengate.isLegacyPair" -}}
{{- eq (include "goldengate.deploymentModel" .) "legacyPair" -}}
{{- end }}

{{- define "goldengate.isSingleRuntime" -}}
{{- eq (include "goldengate.deploymentModel" .) "singleRuntime" -}}
{{- end }}

{{- define "goldengate.runtimeName" -}}
{{- if .Values.runtime.fullnameOverride }}
{{- .Values.runtime.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if .Values.runtime.name }}
{{- .Values.runtime.name | trunc 63 | trimSuffix "-" -}}
{{- else }}
{{- fail "runtime.name (or runtime.fullnameOverride) is required when deploymentModel=singleRuntime." }}
{{- end }}
{{- end }}

{{- define "goldengate.runtimeHeadlessName" -}}
{{- printf "%s-headless" (include "goldengate.runtimeName" .) | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "goldengate.runtimeServiceAccountName" -}}
{{- if .Values.runtime.serviceAccount.create }}
{{- default (printf "%s-sa" (include "goldengate.runtimeName" .)) .Values.runtime.serviceAccount.name | trunc 63 | trimSuffix "-" -}}
{{- else }}
{{- default "default" .Values.runtime.serviceAccount.name -}}
{{- end }}
{{- end }}

{{- define "goldengate.runtimeU02PVCName" -}}
{{- if .Values.runtime.storage.u02.claimName }}
{{- .Values.runtime.storage.u02.claimName | trunc 63 | trimSuffix "-" -}}
{{- else }}
{{- printf "%s-u02" (include "goldengate.runtimeName" .) | trunc 63 | trimSuffix "-" -}}
{{- end }}
{{- end }}

{{- define "goldengate.runtimeAdminProviderClassName" -}}
{{- default (printf "%s-admin" (include "goldengate.runtimeName" .)) .Values.runtime.csi.admin.providerClassName | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "goldengate.runtimeAdminSecretName" -}}
{{- default (printf "%s-admin" (include "goldengate.runtimeName" .)) .Values.runtime.csi.admin.secretName | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "goldengate.runtimeCertificateProviderClassName" -}}
{{- default (printf "%s-certificate" (include "goldengate.runtimeName" .)) .Values.runtime.csi.certificate.providerClassName | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "goldengate.runtimeIngressName" -}}
{{- printf "%s-ingress" (include "goldengate.runtimeName" .) | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- /*
Selector labels are intentionally minimal and stable: name + Helm release
instance is already unique per singleRuntime release (each singleRuntime
release has its own unique Release.Name), and StatefulSet/Service selectors
must never change across template edits (Kubernetes selectors are immutable
on existing StatefulSets). app.kubernetes.io/name is the fixed literal
"goldengate" here -- matching goldengate.runtimeLabels below exactly, so
composing the two never produces a duplicate key with a conflicting value.
Deliberately no "source"/"target"/component value here -- a single-runtime
pod is neither.
*/ -}}
{{- define "goldengate.runtimeSelectorLabels" -}}
app.kubernetes.io/name: goldengate
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- /*
Composes goldengate.runtimeSelectorLabels exactly once (the idiomatic Helm
"labels include selectorLabels" pattern) and adds the remaining descriptive
labels. Never redeclare app.kubernetes.io/name or app.kubernetes.io/instance
here -- callers that need the full label set must use this helper alone,
never this helper plus goldengate.runtimeSelectorLabels together in the same
mapping, or the two app.kubernetes.io/name keys would collide.
*/ -}}
{{- define "goldengate.runtimeLabels" -}}
{{ include "goldengate.runtimeSelectorLabels" . }}
app.kubernetes.io/component: runtime
app.kubernetes.io/part-of: goldengate
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/engine: {{ .Values.runtime.deploymentType | quote }}
goldengate.adcb/environment: {{ .Values.global.environment | quote }}
goldengate.adcb/deployment-name: {{ include "goldengate.runtimeName" . | quote }}
goldengate.adcb/deployment-type: {{ .Values.runtime.deploymentType | quote }}
goldengate.adcb/business-domain: {{ .Values.runtime.businessDomain | quote }}
{{- end }}