{{- define "probe.labels" -}}
app.kubernetes.io/name: pcai-inference-probe
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
hpe-ezua/type: vendor-service
hpe-ezua/app: {{ .Release.Name }}
{{- end -}}
