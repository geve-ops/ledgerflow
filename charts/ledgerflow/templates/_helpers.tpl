{{- define "ledgerflow.labels" -}}
app.kubernetes.io/part-of: ledgerflow
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}

{{- define "ledgerflow.selector" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end }}

{{- define "ledgerflow.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end }}

{{/* Pod-level settings that satisfy the "restricted" Pod Security Standard. */}}
{{- define "ledgerflow.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "ledgerflow.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end }}

{{/* Env shared by every container that talks to Postgres and Redis. */}}
{{- define "ledgerflow.env" -}}
- name: LEDGER_DATABASE_URL
  valueFrom:
    secretKeyRef: {name: {{ .Values.secrets.database }}, key: uri}
- name: DB_USER
  valueFrom:
    secretKeyRef: {name: {{ .Values.secrets.database }}, key: username}
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef: {name: {{ .Values.secrets.database }}, key: password}
- name: LEDGER_DATABASE_RO_URL
  value: "postgresql://$(DB_USER):$(DB_PASSWORD)@{{ .Values.db.roHost }}:{{ .Values.db.port }}/{{ .Values.db.name }}"
- name: LEDGER_REDIS_URL
  value: {{ .Values.redis.url | quote }}
- name: LEDGER_REDIS_PASSWORD
  valueFrom:
    secretKeyRef: {name: {{ .Values.secrets.app }}, key: redis-password}
{{- end }}
