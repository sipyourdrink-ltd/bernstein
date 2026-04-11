{{/*
Expand the name of the chart.
*/}}
{{- define "bernstein.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "bernstein.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart label.
*/}}
{{- define "bernstein.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "bernstein.labels" -}}
helm.sh/chart: {{ include "bernstein.chart" . }}
{{ include "bernstein.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "bernstein.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bernstein.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Server URL used by orchestrator and workers to reach the task server.
*/}}
{{- define "bernstein.serverURL" -}}
http://{{ include "bernstein.fullname" . }}-server:{{ .Values.server.service.port }}
{{- end }}

{{/*
PostgreSQL DSN.
*/}}
{{- define "bernstein.databaseURL" -}}
{{- if .Values.postgresql.enabled -}}
postgresql://{{ .Values.postgresql.auth.username }}:{{ .Values.postgresql.auth.password }}@{{ include "bernstein.fullname" . }}-postgresql:5432/{{ .Values.postgresql.auth.database }}
{{- else -}}
{{ .Values.externalDatabase.url }}
{{- end }}
{{- end }}

{{/*
Redis URL.
*/}}
{{- define "bernstein.redisURL" -}}
{{- if .Values.redis.enabled -}}
redis://{{ include "bernstein.fullname" . }}-redis-master:6379/0
{{- else -}}
{{ .Values.externalRedis.url }}
{{- end }}
{{- end }}

{{/*
Name of the secret holding the auth token.
*/}}
{{- define "bernstein.authSecretName" -}}
{{- if .Values.auth.existingSecret -}}
{{ .Values.auth.existingSecret }}
{{- else -}}
{{ include "bernstein.fullname" . }}-auth
{{- end }}
{{- end }}

{{/*
gRPC server URL used by workers and orchestrator for internal comms.
*/}}
{{- define "bernstein.grpcURL" -}}
{{ include "bernstein.fullname" . }}-grpc:{{ .Values.grpc.port }}
{{- end }}

{{/*
Name of the secret holding LLM provider API keys.
*/}}
{{- define "bernstein.providerKeysSecretName" -}}
{{- if .Values.providerKeys.existingSecret -}}
{{ .Values.providerKeys.existingSecret }}
{{- else -}}
{{ include "bernstein.fullname" . }}-provider-keys
{{- end }}
{{- end }}
