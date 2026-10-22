FROM docker.io/library/python:3.13-slim-bookworm@sha256:a881a81282cca8c57f97014867fa651def1b3799030cc6b1cff45ed55ba8ef36
LABEL org.opencontainers.image.source="https://github.com/enthus-appdev/pcai-inference-probe" \
      org.opencontainers.image.title="pcai-inference-probe" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NVIDIA_VISIBLE_DEVICES=void
WORKDIR /app
COPY app.py index.html app.js ./
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["python3", "/app/app.py"]
