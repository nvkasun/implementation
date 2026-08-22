{{- /*
EFS enforces a 100-character path limit on the access point root path,
which EFS CSI builds from storageClass.basePath + "/" + PVC name +
"-" + a unique suffix (ensureUniqueDirectory). basePath and the PVC name
are the only two segments the chart controls, so both default to short
forms: the deployment is still uniquely identified by basePath (scoped
per runtime name).
*/ -}}

{{- define "goldengate.efsStorageClassName" -}}
{{- if .Values.persistence.efs.storageClass.name }}
{{- .Values.persistence.efs.storageClass.name | trunc 63 | trimSuffix "-" -}}
{{- else }}
{{- printf "gg-efs-%s-%s" .Values.global.environment (include "goldengate.runtimeName" .) | trunc 63 | trimSuffix "-" -}}
{{- end }}
{{- end }}

{{- define "goldengate.efsBasePath" -}}
{{- if .Values.persistence.efs.storageClass.basePath }}
{{- .Values.persistence.efs.storageClass.basePath -}}
{{- else }}
{{- printf "/%s" (include "goldengate.runtimeName" .) -}}
{{- end }}
{{- end }}

{{- define "goldengate.deploymentModel" -}}
{{- .Values.deploymentModel -}}
{{- end }}

{{- define "goldengate.isSingleRuntime" -}}
{{- eq (include "goldengate.deploymentModel" .) "singleRuntime" -}}
{{- end }}

{{- /*
Chart-wide validation: only deploymentModel=singleRuntime is supported.
Called unconditionally from the very top of runtime-statefulset.yaml (the
one template guaranteed to be evaluated on every render) so a missing/
unsupported deploymentModel is rejected with a clear, specific error before
any resource-specific enabled-flag logic runs -- never a silently empty
render. GoldenGate Runtime Presence Contract Finalization: runtime.enabled
was retired as a master runtime-presence switch -- the Helm release itself
is now the presence boundary, so every runtime-statefulset.yaml/service/
serviceaccount/secretproviderclass/pvc/ingress template renders whenever
this chart is rendered at all, subject only to its own resource-specific
feature flags (persistence.enabled, ingress.enabled, runtime.csi.enabled,
etc.), never a second master switch. legacyPair rendering (source/target
StatefulSets, combined Ingress, per-role PVCs, Namespace creation) was
removed once the retired legacy deployment was fully retired; its
implementation remains available through Git history if ever needed.
*/ -}}
{{- define "goldengate.assertSupportedDeploymentModel" -}}
{{- $model := include "goldengate.deploymentModel" . }}
{{- if eq $model "singleRuntime" }}
{{- else if eq $model "legacyPair" }}
{{- fail "deploymentModel=legacyPair is no longer supported by this chart. legacyPair rendering was removed after the retired legacy deployment was fully retired -- see Git history for the removed implementation. singleRuntime is the only supported deploymentModel." }}
{{- else }}
{{- fail (printf "Unsupported or missing deploymentModel %q. Only \"singleRuntime\" is supported." $model) }}
{{- end }}
{{- end }}

{{- /*
The runtime folder basename / Helm release name IS the runtime identity
(envs/dev/<name>/values.yaml, Release.Name=<name>). Every singleRuntime
resource name/label/env var derives from this one helper -- runtime.name
is only an escape hatch, never required.
*/ -}}
{{- define "goldengate.runtimeName" -}}
{{- if .Values.runtime.fullnameOverride }}
{{- .Values.runtime.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if .Values.runtime.name }}
{{- .Values.runtime.name | trunc 63 | trimSuffix "-" -}}
{{- else }}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end }}
{{- end }}

{{- define "goldengate.runtimeIngressHost" -}}
{{- if .Values.ingress.host }}
{{- .Values.ingress.host -}}
{{- else }}
{{- printf "%s.%s" (include "goldengate.runtimeName" .) .Values.ingress.hostDomain -}}
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