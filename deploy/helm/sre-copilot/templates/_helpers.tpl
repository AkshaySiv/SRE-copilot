{{- define "sre-copilot.fullname" -}}{{- printf "%s" .Release.Name | trunc 63 | trimSuffix "-" -}}{{- end -}}
{{- define "sre-copilot.labels" -}}
app.kubernetes.io/name: sre-copilot
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}
{{- define "sre-copilot.selector" -}}
app.kubernetes.io/name: sre-copilot
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "sre-copilot.sa" -}}{{- default (include "sre-copilot.fullname" .) .Values.serviceAccount.name -}}{{- end -}}
{{- define "sre-copilot.secret" -}}{{- default (include "sre-copilot.fullname" .) .Values.existingSecret -}}{{- end -}}
