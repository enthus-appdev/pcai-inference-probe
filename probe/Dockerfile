FROM docker.io/library/python:3.15-slim-bookworm@sha256:4e03260adeeebafccd084f3584e6b84823aba5651cfb53ef8a078ed441ddf9cd
LABEL org.opencontainers.image.source="https://github.com/enthus-appdev/pcai-inference-probe" \
      org.opencontainers.image.title="pcai-inference-probe" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NVIDIA_VISIBLE_DEVICES=void
WORKDIR /app
COPY app.py index.html app.js ./
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["python3", "/app/app.py"]
